from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
import chromadb
import json
import os
import re
import asyncio
import threading
import hashlib
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load .env before any os.getenv() calls below (e.g. the Gemini API key).
load_dotenv()

# Import chat router
from chat import router as chat_router
# Import WhatsApp transport module (Meta Cloud API webhooks + outbound sends)
from whatsapp import router as whatsapp_router, set_ai_handler, set_receipt_handler

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "healthy", "service": "Loamy Backend"}

# Include chat routes
app.include_router(chat_router)
# Include WhatsApp webhook routes
app.include_router(whatsapp_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# OWNER KILL SWITCH
# ============================================
# A single owner-controlled flag that can disable the entire backend on demand.
# Because the value lives ONLY in the environment (never in git), only whoever
# controls the deployment env can flip it. Set LOAMY_KILL_SWITCH=on to make the
# API refuse all traffic; unset it (or any other value) to run normally.
# This is single-purpose and dependency-free (Atomic Modularity rule).
from fastapi.responses import JSONResponse
from starlette.requests import Request as _StarletteRequest


def _kill_switch_active() -> bool:
    return os.getenv("LOAMY_KILL_SWITCH", "").strip().lower() in ("on", "true", "1", "yes")


@app.middleware("http")
async def owner_kill_switch(request: _StarletteRequest, call_next):
    # /health stays reachable so you can confirm the server itself is up while
    # the app logic is intentionally disabled.
    if _kill_switch_active() and request.url.path != "/health":
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "data": None,
                "error": "Service temporarily disabled by the owner.",
            },
        )
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "success", "data": {"disabled": _kill_switch_active()}, "error": None}

# --- FIX: MOVE THE VAULT OUTSIDE THE PROJECT FOLDER ---
# By using ../ we save the database one folder UP, so Live Server won't see it and refresh your page!
db_path = os.path.join(os.path.dirname(__file__), "..", "fintech_ai_vault_hidden")
client = chromadb.PersistentClient(path=db_path)
collection = client.get_or_create_collection(name="user_transactions")

goals_collection = client.get_or_create_collection(name="user_goals")
accounts_collection = client.get_or_create_collection(name="user_accounts")
expenses_collection = client.get_or_create_collection(name="user_expenses")
chats_collection = client.get_or_create_collection(name="user_chats") # Added to retrieve history
users_collection = client.get_or_create_collection(name="users") # For multi-user authentication
gmail_data_collection = client.get_or_create_collection(name="gmail_data")  # For Gmail email data
pending_conversions_collection = client.get_or_create_collection(name="pending_conversions")  # For tracking currency estimates awaiting bank true-up
invoices_collection = client.get_or_create_collection(name="invoices")  # For tracking money owed (invoices)
review_queue_collection = client.get_or_create_collection(name="review_queue")  # Unrecognized bank transactions awaiting user clarification
vendor_memory_collection = client.get_or_create_collection(name="vendor_memory")  # Learned vendor -> category rules (Smart Memory)
notifications_collection = client.get_or_create_collection(name="notifications")  # In-app notifications (e.g. daily 5pm invoice reminder)
sync_state_collection = client.get_or_create_collection(name="sync_state")  # Delta-sync bookmarks: last_synced_timestamp per user
gmail_credentials_collection = client.get_or_create_collection(name="gmail_credentials")  # Per-user Gmail refresh tokens for server-side auto-sync


# ============================================
# BACKGROUND SCHEDULER (daily 5pm invoice reminder)
# ============================================
# NOTE on bank-data sync: the freshest bank data can only be pulled from the
# BROWSER, because the Gmail OAuth access token lives in the client's
# localStorage (set during the Gmail connect flow). The server has no token, so
# a server-side timer cannot fetch new emails. The real activity-driven sync is
# therefore implemented on the frontend (see src/services/activitySyncService.js
# + src/hooks/useGlobalActivitySync.js). This scheduler only handles work the
# server CAN do on its own, like the daily invoice reminder below.


# --- Daily 5pm invoice reminder -----------------------------------------------
# Single responsibility: create ONE in-app notification per day asking the user
# whether they have an invoice to log. Idempotent - re-running on the same day
# will not create duplicates.
INVOICE_REMINDER_HOUR = 17  # 5 PM, 24-hour clock


def create_invoice_reminder():
    """Create today's invoice reminder notification if it doesn't exist yet."""
    try:
        from datetime import datetime as _dt
        user_id = "default"
        today = _dt.now().strftime("%Y-%m-%d")
        notif_id = f"invoice_reminder_{user_id}_{today}"

        # Idempotency guard - don't double-create for the same day.
        existing = notifications_collection.get(ids=[notif_id])
        if existing["ids"]:
            return {"created": False, "reason": "already_exists", "id": notif_id}

        notifications_collection.add(
            ids=[notif_id],
            documents=["Daily 5pm invoice reminder"],
            metadatas=[{
                "user_id": user_id,
                "type": "invoice_reminder",
                "title": "Any invoices to log today?",
                "message": "It's 5 PM. Do you have an invoice you'd like to log before the day ends?",
                "status": "unread",
                "action": "create_invoice",
                "created_at": _dt.now().isoformat(),
                "date": today,
            }]
        )
        print(f"[Reminder] Created invoice reminder {notif_id}")
        return {"created": True, "id": notif_id}
    except Exception as e:
        print(f"[Reminder] create_invoice_reminder failed: {e}")
        return {"created": False, "error": str(e)}


def _invoice_reminder_job():
    """Wrapper the scheduler calls every day at 5 PM."""
    print("[Reminder] Running daily 5pm invoice reminder job...")
    create_invoice_reminder()


# Build the scheduler lazily so a missing apscheduler install can't crash import.
scheduler = None
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(daemon=True)
    # Daily invoice reminder at 5:00 PM local server time.
    scheduler.add_job(
        _invoice_reminder_job,
        CronTrigger(hour=INVOICE_REMINDER_HOUR, minute=0),
        id="daily_invoice_reminder",
        replace_existing=True,
    )
except Exception as e:  # pragma: no cover - environment without apscheduler
    print(f"[Scheduler] APScheduler unavailable, scheduled jobs disabled: {e}")


@app.on_event("startup")
def _start_scheduler():
    """Start the background scheduler cleanly on app startup."""
    if scheduler and not scheduler.running:
        scheduler.start()
        print(
            f"[Scheduler] Started. Daily invoice reminder set for "
            f"{INVOICE_REMINDER_HOUR}:00."
        )


@app.on_event("shutdown")
def _stop_scheduler():
    """Shut the scheduler down cleanly on app termination."""
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        print("[Scheduler] Stopped.")


# ============================================
# NOTIFICATIONS (in-app reminders, e.g. the daily 5pm invoice nudge)
# ============================================
# Single responsibility per endpoint. The frontend polls /notifications, shows a
# banner, and POSTs back to dismiss. The 5pm reminder itself is created by the
# scheduler's create_invoice_reminder() job above.

@app.get("/notifications")
async def get_notifications(user_id: str = "default"):
    """Return this user's notifications, newest first."""
    try:
        results = notifications_collection.get(where={"user_id": user_id})
        items = []
        for i in range(len(results["ids"])):
            meta = results["metadatas"][i] or {}
            items.append({
                "id": results["ids"][i],
                "type": meta.get("type", "info"),
                "title": meta.get("title", ""),
                "message": meta.get("message", ""),
                "status": meta.get("status", "unread"),
                "action": meta.get("action", ""),
                "created_at": meta.get("created_at", ""),
                "date": meta.get("date", ""),
            })
        # Newest first; only surface ones the user hasn't dismissed.
        items = [it for it in items if it["status"] != "dismissed"]
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return {
            "status": "success",
            "data": {"items": items, "unread_count": sum(1 for it in items if it["status"] == "unread")},
            "error": None,
        }
    except Exception as e:
        return {"status": "error", "data": {"items": [], "unread_count": 0}, "error": str(e)}


@app.post("/notifications/dismiss")
async def dismiss_notification(data: dict):
    """Mark a notification as dismissed so it stops showing."""
    try:
        notif_id = data.get("notification_id")
        if not notif_id:
            return {"status": "error", "data": None, "error": "notification_id is required"}
        result = notifications_collection.get(ids=[notif_id])
        if not result["ids"]:
            return {"status": "error", "data": None, "error": "Notification not found"}
        meta = result["metadatas"][0] or {}
        meta["status"] = "dismissed"
        notifications_collection.update(ids=[notif_id], metadatas=[meta])
        return {"status": "success", "data": {"id": notif_id}, "error": None}
    except Exception as e:
        return {"status": "error", "data": None, "error": str(e)}


@app.post("/notifications/trigger-invoice-reminder")
async def trigger_invoice_reminder():
    """Manually fire today's invoice reminder (handy for testing the 5pm nudge)."""
    result = create_invoice_reminder()
    return {"status": "success", "data": result, "error": None}


# ============================================
# CURRENCY CONVERSION SYSTEM (Step A: Instant Estimate)
# ============================================
# Uses free ExchangeRate-API for real-time conversion
# Estimates are later "true'd up" when bank alerts arrive

EXCHANGE_RATE_API_URL = "https://api.exchangerate-api.com/v4/latest/"

# Cache exchange rates for 1 hour to avoid excessive API calls
_exchange_rate_cache = {}
_cache_timestamp = None

def get_exchange_rate(from_currency: str, to_currency: str = "NGN") -> float:
    """
    Fetch real-time exchange rate from ExchangeRate-API (free tier).
    Returns the conversion rate: 1 from_currency = X to_currency
    """
    import requests
    from datetime import datetime, timedelta
    
    global _exchange_rate_cache, _cache_timestamp
    
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    cache_key = f"{from_currency}_{to_currency}"
    
    # Check cache (valid for 1 hour)
    if _cache_timestamp and datetime.now() - _cache_timestamp < timedelta(hours=1):
        if cache_key in _exchange_rate_cache:
            return _exchange_rate_cache[cache_key]
    
    try:
        response = requests.get(f"{EXCHANGE_RATE_API_URL}{from_currency}", timeout=5)
        if response.status_code == 200:
            data = response.json()
            rate = data.get("rates", {}).get(to_currency, None)
            if rate:
                # Update cache
                _exchange_rate_cache[cache_key] = rate
                _cache_timestamp = datetime.now()
                return rate
    except Exception as e:
        print(f"Exchange rate API error: {e}")
    
    # Fallback rates if API fails (approximate as of May 2026)
    fallback_rates = {
        "USD_NGN": 1550.0,
        "EUR_NGN": 1680.0,
        "GBP_NGN": 1950.0,
        "CAD_NGN": 1150.0,
        "AUD_NGN": 1020.0,
        "ZAR_NGN": 85.0,
        "KES_NGN": 12.0,
        "GHS_NGN": 130.0,
        "INR_NGN": 18.5,   # Indian Rupee
        "MUR_NGN": 34.0,   # Mauritian Rupee
    }
    return fallback_rates.get(cache_key, 1.0)


def detect_currency_from_amount(amount_str: str, body: str = "") -> tuple:
    """
    Detect currency from amount string or email body.
    Returns: (amount_float, currency_code, original_symbol)
    """
    amount_str = str(amount_str).strip()
    body_lower = body.lower()
    
    # Currency symbol patterns
    currency_patterns = [
        (r'\$\s*([\d,]+\.?\d*)', 'USD', '$'),
        (r'USD\s*([\d,]+\.?\d*)', 'USD', '$'),
        (r'€\s*([\d,]+\.?\d*)', 'EUR', '€'),
        (r'EUR\s*([\d,]+\.?\d*)', 'EUR', '€'),
        (r'£\s*([\d,]+\.?\d*)', 'GBP', '£'),
        (r'GBP\s*([\d,]+\.?\d*)', 'GBP', '£'),
        (r'₦\s*([\d,]+\.?\d*)', 'NGN', '₦'),
        (r'NGN\s*([\d,]+\.?\d*)', 'NGN', '₦'),
        (r'N\s*([\d,]+\.?\d*)', 'NGN', '₦'),  # Nigerian shorthand
        (r'R\s*([\d,]+\.?\d*)', 'ZAR', 'R'),  # South African Rand
        (r'KES\s*([\d,]+\.?\d*)', 'KES', 'KES'),
        (r'GHS\s*([\d,]+\.?\d*)', 'GHS', 'GHS'),
    ]
    
    for pattern, currency, symbol in currency_patterns:
        match = re.search(pattern, amount_str, re.IGNORECASE)
        if match:
            amount = float(match.group(1).replace(',', ''))
            return (amount, currency, symbol)
    
    # Check body for currency context clues
    if 'usd' in body_lower or 'dollar' in body_lower or '$' in body:
        # Try to extract just the number
        num_match = re.search(r'([\d,]+\.?\d*)', amount_str)
        if num_match:
            return (float(num_match.group(1).replace(',', '')), 'USD', '$')
    
    if 'eur' in body_lower or 'euro' in body_lower or '€' in body:
        num_match = re.search(r'([\d,]+\.?\d*)', amount_str)
        if num_match:
            return (float(num_match.group(1).replace(',', '')), 'EUR', '€')
    
    # Default: assume NGN if no currency detected and sender is Nigerian
    num_match = re.search(r'([\d,]+\.?\d*)', amount_str)
    if num_match:
        return (float(num_match.group(1).replace(',', '')), 'NGN', '₦')
    
    return (0.0, 'NGN', '₦')


def convert_to_local_currency(amount: float, from_currency: str, to_currency: str = "NGN") -> dict:
    """
    Convert foreign currency to local currency with exchange rate info.
    Returns: {
        "original_amount": float,
        "original_currency": str,
        "converted_amount": float,
        "converted_currency": str,
        "exchange_rate": float,
        "is_estimate": bool  # True until bank confirms
    }
    """
    if from_currency.upper() == to_currency.upper():
        return {
            "original_amount": amount,
            "original_currency": from_currency,
            "converted_amount": amount,
            "converted_currency": to_currency,
            "exchange_rate": 1.0,
            "is_estimate": False
        }
    
    rate = get_exchange_rate(from_currency, to_currency)
    converted = round(amount * rate, 2)
    
    return {
        "original_amount": amount,
        "original_currency": from_currency,
        "converted_amount": converted,
        "converted_currency": to_currency,
        "exchange_rate": rate,
        "is_estimate": True  # Marked as estimate until bank confirms
    }


def save_pending_conversion(transaction_id: str, original_amount: float, original_currency: str,
                           estimated_ngn: float, vendor: str, date: str):
    """
    Save a pending currency conversion for later true-up when bank alert arrives.
    This allows matching foreign transactions to bank debits.
    """
    try:
        pending_conversions_collection.add(
            ids=[transaction_id],
            documents=[f"{vendor} {original_currency}{original_amount} estimated ₦{estimated_ngn}"],
            metadatas=[{
                "original_amount": original_amount,
                "original_currency": original_currency,
                "estimated_ngn": estimated_ngn,
                "vendor": vendor,
                "date": date,
                "status": "pending",  # pending | matched | expired
                "created_at": datetime.now().isoformat()
            }]
        )
        return True
    except Exception as e:
        print(f"Error saving pending conversion: {e}")
        return False


# ============================================
# STEP B: BANK ALERT TRUE-UP SYSTEM
# ============================================
# When a bank alert arrives, check if it matches any pending estimates

def find_matching_pending_conversion(bank_amount: float, bank_date: str, tolerance_percent: float = 10.0):
    """
    Find a pending currency conversion that might match this bank debit.
    Uses fuzzy matching: bank amount within X% of estimate + same day or day before.
    
    Returns: matched pending conversion or None
    """
    try:
        results = pending_conversions_collection.get(
            where={"status": "pending"}
        )
        
        if not results['ids']:
            return None
        
        # Parse bank date
        bank_date_obj = None
        for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"]:
            try:
                bank_date_obj = datetime.strptime(bank_date[:10], fmt)
                break
            except:
                continue
        
        for i, meta in enumerate(results['metadatas']):
            estimated = float(meta.get('estimated_ngn', 0))
            
            # Check amount tolerance (within X%)
            lower_bound = estimated * (1 - tolerance_percent / 100)
            upper_bound = estimated * (1 + tolerance_percent / 100)
            
            if lower_bound <= bank_amount <= upper_bound:
                # Check date proximity (same day or 1 day before/after)
                pending_date = meta.get('date', '')
                if pending_date and bank_date_obj:
                    try:
                        for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"]:
                            try:
                                pending_date_obj = datetime.strptime(pending_date[:10], fmt)
                                break
                            except:
                                continue
                        
                        day_diff = abs((bank_date_obj - pending_date_obj).days)
                        if day_diff <= 2:  # Within 2 days
                            return {
                                "id": results['ids'][i],
                                "metadata": meta,
                                "match_confidence": 1.0 - (abs(bank_amount - estimated) / estimated)
                            }
                    except:
                        pass
                
                # If dates can't be compared, still return if amount matches closely
                if abs(bank_amount - estimated) / estimated < 0.05:  # Within 5%
                    return {
                        "id": results['ids'][i],
                        "metadata": meta,
                        "match_confidence": 0.8
                    }
        
        return None
        
    except Exception as e:
        print(f"Error finding matching conversion: {e}")
        return None


def true_up_conversion(pending_id: str, actual_bank_amount: float, bank_reference: str = ""):
    """
    Update a pending conversion with the actual bank amount (true-up).
    This replaces the estimate with the real cost from the bank.
    """
    try:
        # Get the pending conversion
        result = pending_conversions_collection.get(ids=[pending_id])
        if not result['ids']:
            return False
        
        old_meta = result['metadatas'][0]
        
        # Update with actual amount
        pending_conversions_collection.update(
            ids=[pending_id],
            metadatas=[{
                **old_meta,
                "status": "matched",
                "actual_bank_amount": actual_bank_amount,
                "bank_reference": bank_reference,
                "true_up_difference": actual_bank_amount - float(old_meta.get('estimated_ngn', 0)),
                "matched_at": datetime.now().isoformat()
            }]
        )
        
        print(f"True-up complete: Estimate ₦{old_meta.get('estimated_ngn')} → Actual ₦{actual_bank_amount}")
        return True
        
    except Exception as e:
        print(f"Error during true-up: {e}")
        return False

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("Missing GEMINI_API_KEY. Add it to your .env file.")
genai.configure(api_key=API_KEY)

# Using gemini-2.5-flash for faster responses with good context
model = genai.GenerativeModel('gemini-2.5-flash')

SYSTEM_PROMPT = """
You are a Financial Data Extractor for an African fintech app. 
Return ONLY a JSON object (no markdown, no text) with this structure:
{
  "vendor": "Store Name",
  "date": "YYYY-MM-DD",
  "total": 0.00,
  "currency": "NGN",
  "category": "Food/Transport/Etc",
  "items": [{"name": "item", "price": 0.00}],
  "dpc_logic": "Detailed math verification: (Item1 + Item2 + Tax = Total)"
}

IMPORTANT CURRENCY RULES:
- Default to "NGN" (Nigerian Naira) for receipts from Nigerian vendors
- Look for currency symbols: ₦ or NGN = "NGN", $ = "USD", € = "EUR", £ = "GBP", ₹ or Rs or INR = "INR", ₨ or MUR = "MUR"
- If prices look like Nigerian amounts (thousands without cents, e.g., 1200, 4500, 8500), use "NGN"
- Common Nigerian receipt indicators: VAT 7.5%, Nigerian phone numbers, .ng domains
- For USD receipts (Vercel, AWS, GitHub, etc.), return "USD"
- For Indian/Mauritian receipts (Rs, ₹), return "INR" or "MUR"
- Return the currency code in the "currency" field
"""

def to_sentence_case(text):
    if not text:
        return ""
    text = text.strip()
    return text[0].upper() + text[1:]
    
def process_receipt_image(file_data, content_type, user_id="default"):
    """Shared receipt pipeline used by BOTH the web /upload-artifact endpoint and
    the WhatsApp image handler (whatsapp.py). Extracts structured data from a
    receipt image/PDF with Gemini vision, converts any foreign currency to NGN,
    and saves it as an expense transaction scoped to user_id. Returns the standard
    response dict.

    Kept channel-agnostic (and synchronous) so a receipt behaves identically
    whether it arrives from the web uploader or a WhatsApp photo; callers run it
    via asyncio.to_thread so the blocking Gemini + Supabase work never stalls the
    event loop (Atomic Modularity + Logic/UI Separation rules)."""
    try:
        # Multimodal call to Gemini
        response = model.generate_content(
            [SYSTEM_PROMPT, {"mime_type": content_type, "data": file_data}],
            generation_config={"response_mime_type": "application/json"}
        )
        
        # Parse the JSON directly
        transaction_data = json.loads(response.text)
        
        # --- CURRENCY DETECTION & CONVERSION (Step A) ---
        # First, use AI-detected currency from the response
        ai_detected_currency = transaction_data.get('currency', '').upper()
        original_total = transaction_data.get('total', 0)
        
        # Map currency to symbol for display
        currency_symbols = {
            'NGN': '₦', 'USD': '$', 'EUR': '€', 'GBP': '£',
            'ZAR': 'R', 'KES': 'KES', 'GHS': 'GH₵',
            'INR': '₹', 'MUR': '₨'  # Indian Rupee, Mauritian Rupee
        }
        
        # Determine currency - prioritize AI detection
        if ai_detected_currency in currency_symbols:
            original_currency = ai_detected_currency
        else:
            # Fallback: detect from total string
            total_str = str(original_total)
            if '$' in total_str:
                original_currency = "USD"
            elif '€' in total_str:
                original_currency = "EUR"
            elif '£' in total_str:
                original_currency = "GBP"
            elif '₹' in total_str or 'Rs' in total_str or 'INR' in total_str.upper():
                original_currency = "INR"
            elif '₨' in total_str or 'MUR' in total_str.upper():
                original_currency = "MUR"
            else:
                original_currency = "NGN"  # Default to NGN for Nigerian app
        
        is_foreign_currency = original_currency != "NGN"
        converted_amount = float(original_total) if isinstance(original_total, (int, float)) else 0
        conversion_info = None
        
        # Extract numeric value
        numeric_total = float(re.sub(r'[^\d.]', '', str(original_total))) if original_total else 0
        
        # Convert foreign currency to NGN
        if is_foreign_currency and original_currency != "NGN":
            conversion_info = convert_to_local_currency(numeric_total, original_currency, "NGN")
            converted_amount = conversion_info["converted_amount"]
            
            # Save as pending conversion for future bank alert true-up
            transaction_id = f"conv_{os.urandom(4).hex()}"
            save_pending_conversion(
                transaction_id=transaction_id,
                original_amount=numeric_total,
                original_currency=original_currency,
                estimated_ngn=converted_amount,
                vendor=transaction_data.get('vendor', 'Unknown'),
                date=transaction_data.get('date', datetime.now().strftime('%Y-%m-%d'))
            )
            print(f"Currency Conversion: {original_currency}{numeric_total} → ₦{converted_amount} (estimate)")
        else:
            converted_amount = numeric_total

        # --- NORMALIZE THE DATE ---
        # The receipt's printed date can be missing or in a non-ISO/foreign
        # format (e.g. a Carrefour Mauritius slip). Keep it only if it parses to
        # a real calendar date; otherwise fall back to today so a just-scanned
        # receipt reliably counts toward "spent today". Store as clean ISO.
        parsed_date = database._to_date_obj(transaction_data.get('date'))
        effective_date = (parsed_date or datetime.now().date()).isoformat()
        transaction_data['date'] = effective_date

        # --- SAVE TO SUPABASE (scoped to this user) ---
        # Store with both original and converted amounts. Human-readable summary
        # goes in `document`; the deterministic numbers go in typed columns.
        doc_text = f"Spent ₦{converted_amount} at {transaction_data['vendor']} on {transaction_data['date']}. Items: {transaction_data['items']}"
        if is_foreign_currency:
            doc_text += f" (Original: {original_currency}{numeric_total})"

        tx_id = f"trans_{os.urandom(4).hex()}"
        save_res = database.add_transaction(
            user_id=user_id,
            tx_id=tx_id,
            amount=converted_amount,          # stored in NGN
            vendor=transaction_data['vendor'],
            original_amount=numeric_total,
            original_currency=original_currency,
            currency="NGN",
            date=transaction_data['date'],
            category=transaction_data['category'],
            transaction_type="expense",
            is_estimate=is_foreign_currency,  # True until a bank alert trues it up
            document=doc_text,
        )
        if save_res["status"] != "success":
            print(f"[upload-artifact] Supabase save failed: {save_res['error']}")
            return {"error": f"Could not save transaction: {save_res['error']}"}
        print(f"Supabase Updated: {transaction_data['vendor']} - ₦{converted_amount}")

        # Build response with conversion info
        response_data = {
            "status": "success", 
            "analysis": transaction_data,
            "currency_info": {
                "original_amount": numeric_total,
                "original_currency": original_currency,
                "displayed_amount": converted_amount,
                "displayed_currency": "NGN",
                "is_estimate": is_foreign_currency
            }
        }
        
        if conversion_info:
            response_data["conversion"] = conversion_info
            response_data["analysis"]["total_ngn"] = converted_amount
            response_data["analysis"]["exchange_rate"] = conversion_info["exchange_rate"]
            response_data["analysis"]["conversion_note"] = f"Converted from {original_currency}{numeric_total} at rate {conversion_info['exchange_rate']}"
        
        return response_data

    except Exception as e:
        print(f"Backend Error: {str(e)}")
        return {"error": str(e)}


@app.post("/upload-artifact")
async def upload_artifact(file: UploadFile = File(...), user_id: str = Form("default")):
    print(f"--- Scanning Artifact: {file.filename} (user={user_id}) ---")
    try:
        file_data = await file.read()
        # Gemini vision + Supabase writes are blocking; run them off the event
        # loop so concurrent requests (chats, dashboard) aren't stalled.
        return await asyncio.to_thread(
            process_receipt_image, file_data, file.content_type, user_id
        )
    except Exception as e:
        print(f"Backend Error: {str(e)}")
        return {"error": str(e)}


@app.post("/chat")
async def chat_with_history(query: dict):
    user_msg = query.get("text")
    user_id = query.get("user_id", "default_user")  # Get user ID for Gmail data lookup
    # "web" (default) keeps the existing markdown-friendly formatting for the
    # web chat UI. "whatsapp" appends the plain-text formatting rules below,
    # since WhatsApp renders a stray asterisk literally instead of bolding it.
    channel = query.get("channel", "web")
    
    # Pre-calculate accurate date information so AI doesn't have to guess
    from calendar import monthrange
    now = datetime.now()
    current_date = now.strftime("%B %d, %Y")  # e.g., "April 08, 2026"
    current_day = now.day
    current_month = now.month
    current_year = now.year
    days_in_current_month = monthrange(current_year, current_month)[1]
    days_left_in_month = days_in_current_month - current_day
    
    # Build date context for AI
    date_context = f"""
    TODAY'S DATE: {current_date}
    CURRENT YEAR: {current_year}
    - Day of month: {current_day}
    - Days in {now.strftime('%B')}: {days_in_current_month}
    - Days remaining in {now.strftime('%B')}: {days_left_in_month} (from {now.strftime('%B')} {current_day + 1} to {now.strftime('%B')} {days_in_current_month})
    
    CRITICAL DATE RULE: When user mentions a date WITHOUT a year (e.g., "April 12th", "June 1st"):
    - ALWAYS default to CURRENT YEAR ({current_year}) or a FUTURE year
    - NEVER use a past year (like 2024 or 2025 if current year is {current_year})
    - If the date has already passed this year, use NEXT year
    """
    
    try:
        # --- NEW: Retrieve Recent Chat History ---
        # This gives the AI short-term memory of what it just proposed
        recent_chat_context = "No previous conversation in this session."
        try:
            chats = chats_collection.get()
            if chats['ids']:
                # Find the most recently updated chat
                latest_chat_idx = max(range(len(chats['ids'])), key=lambda i: chats['metadatas'][i].get('updated_at', ''))
                latest_doc = chats['documents'][latest_chat_idx]
                if latest_doc:
                    messages = json.loads(latest_doc)
                    # Get last 6 messages
                    recent_msgs = []
                    for msg in messages[-6:]:
                        sender = "Advisor" if msg.get("sender") == "ai" else "User"
                        content = msg.get('content', '')
                        recent_msgs.append(f"{sender}: {content}")
                    if recent_msgs:
                        recent_chat_context = "\n\n".join(recent_msgs)
        except Exception as e:
            print(f"Error fetching chat history: {e}")

        # 1. Get this user's spending ledger (receipts/manual entries) from
        #    Supabase. IMPORTANT: this used to read the ChromaDB `collection`,
        #    but nothing has written to that collection since receipts moved to
        #    Supabase (see database.add_transaction) - so a receipt scanned via
        #    WhatsApp or the web uploader could NEVER show up here, no matter
        #    how it was dated. It also compared dates as
        #    "September 11, 2026" == "2026-09-11", which never matches even
        #    when there WAS data. database.get_chat_financial_context() reads
        #    the real table and computes today/week/month totals deterministically
        #    (across receipts AND bank alerts) instead of leaving date math to the AI.
        ledger_ctx_res = database.get_chat_financial_context(user_id)
        ledger_data = (ledger_ctx_res.get("data") or {}) if ledger_ctx_res.get("status") == "success" else {}
        ledger_summary = ledger_data.get("summary", {}) or {}
        ledger_only_entries = [
            t for t in (ledger_data.get("recent_transactions") or [])
            if t.get("source") == "ledger"
        ]

        spending_entries = []
        for t in ledger_only_entries:
            amt = t.get("amount", 0) or 0
            cur = t.get("currency") or "NGN"
            symbol = "₦" if cur == "NGN" else cur + " "
            spending_entries.append(
                f"- {t.get('date')}: {t.get('vendor')} - {symbol}{amt:,.2f} ({t.get('category')})"
            )

        # Build comprehensive spending context. The SPENDING SUMMARY figures are
        # authoritative (combine receipts + bank alerts) - the AI should quote
        # them exactly rather than re-adding the itemized list itself.
        history_context = f"""
        === ALL TRANSACTIONS/RECEIPTS ===
        {chr(10).join(spending_entries) if spending_entries else "No transactions recorded."}
        
        === SPENDING SUMMARY (authoritative - quote exactly) ===
        - Total spent today ({ledger_summary.get('today_date', current_date)}): ₦{ledger_summary.get('spent_today', 0):,.2f}
        - Total spent this week (since Monday): ₦{ledger_summary.get('spent_this_week', 0):,.2f}
        - Total spent this month ({now.strftime('%B')} {current_year}): ₦{ledger_summary.get('spent_this_month', 0):,.2f}
        """
        
        # 2. Search for existing saving goals
        goal_results = goals_collection.query(query_texts=[user_msg], n_results=3)
        goal_docs = goal_results.get('documents', [[]])[0]
        goals_context = "\n".join(goal_docs) if goal_docs else "No active saving goals found."
        
        # 3. Get ALL goals AND bills with amounts for full context
        all_items = goals_collection.get()
        goals_summary = []
        bills_summary = []
        total_assigned = 0
        
        for i in range(len(all_items['ids'])):
            meta = all_items['metadatas'][i]
            item = meta.get('item', 'Unknown')
            amount = float(meta.get('amount', 0))
            assigned = float(meta.get('assigned', 0))
            deadline = meta.get('deadline', 'Not set')
            category = meta.get('category', 'goal')
            total_assigned += assigned
            
            entry = f"- {item}: Target ${amount:.2f}, Assigned ${assigned:.2f}, Deadline: {deadline}"
            if category == 'bill':
                bills_summary.append(entry)
            else:
                goals_summary.append(entry)
        
        goals_full_context = "\n".join(goals_summary) if goals_summary else "No savings goals set."
        bills_full_context = "\n".join(bills_summary) if bills_summary else "No bills tracked."
        
        # Combined context for AI to see everything
        all_items_context = f"""
        === SAVINGS GOALS ===
        {goals_full_context}
        
        === BILLS ===
        {bills_full_context}
        """
        
        # 4. Get THIS user's bank accounts from Supabase (scoped: the old
        #    unscoped ChromaDB read leaked every user's accounts into the prompt).
        accounts_summary = []
        total_balance = 0
        acct_res = database.get_accounts(user_id)
        if acct_res["status"] == "success":
            for acct in acct_res["data"]:
                name = acct.get('account_name') or acct.get('name', 'Unknown')
                balance = float(acct.get('balance', 0) or 0)
                total_balance += balance
                accounts_summary.append(f"- {name}: ₦{balance:,.2f}")
        
        ready_to_assign = total_balance - total_assigned
        accounts_context = "\n".join(accounts_summary) if accounts_summary else "No accounts found."
        accounts_context += f"\n\nTotal Balance: ₦{total_balance:,.2f}"
        accounts_context += f"\nTotal Assigned to Goals/Bills: ₦{total_assigned:,.2f}"
        accounts_context += f"\nReady to Assign (Available): ${ready_to_assign:.2f}"
        
        # 5. Get Gmail email data for spending insights
        gmail_context = ""
        bank_alerts_context = ""
        latest_bank_balance = None
        
        try:
            # Supabase-primary loader (ChromaDB fallback) so chat sees the same
            # emails as the dashboard.
            emails = load_user_emails(user_id)
            if emails:
                # Regular (non-bank-alert) email receipts
                regular_emails = [e for e in emails if not e.get('is_bank_alert')]

                # Format regular email receipts
                gmail_entries = []
                for email in regular_emails[:15]:
                    sender = email.get('sender', 'Unknown')
                    subject = email.get('subject', '')
                    date = email.get('date', '')
                    amount = email.get('amount')
                    email_type = email.get('type', 'receipt')
                    email_currency = email.get('currency', 'NGN')

                    # Auto-detect currency from sender for known USD vendors
                    sender_lower = sender.lower()
                    if any(usd_vendor in sender_lower for usd_vendor in ['vercel', 'aws', 'github', 'stripe', 'digitalocean', 'heroku', 'netlify', 'cloudflare', 'amazon web services']):
                        email_currency = 'USD'

                    if amount:
                        if email_currency != 'NGN':
                            # Foreign currency - show original + NGN equivalent
                            exchange_rate = get_exchange_rate(email_currency, "NGN")
                            ngn_equivalent = amount * exchange_rate
                            currency_symbols = {'USD': '$', 'EUR': '€', 'GBP': '£', 'MUR': '₨', 'INR': '₹'}
                            symbol = currency_symbols.get(email_currency, '$')
                            gmail_entries.append(f"- {date}: {sender} - {subject} ({symbol}{amount:,.2f} = ₦{ngn_equivalent:,.2f}, {email_type})")
                        else:
                            gmail_entries.append(f"- {date}: {sender} - {subject} (₦{amount:,.2f}, {email_type})")
                    else:
                        gmail_entries.append(f"- {date}: {sender} - {subject} ({email_type})")

                # Bank alerts: use the SHARED snapshot so the web chat,
                # WhatsApp, and the dashboard all report the SAME balance and
                # cash flow. (The old code picked the first alert that had a
                # balance, which is why chat disagreed with the dashboard.)
                snap = compute_bank_snapshot(user_id)
                latest_bank_balance = snap["balance"]

                if gmail_entries:
                    gmail_context = f"""
        === EMAIL RECEIPTS & PURCHASES FROM GMAIL ===
        {chr(10).join(gmail_entries)}
        """

                if snap["alert_count"] > 0:
                    bank_alerts_context = f"""
        === BANK TRANSACTION ALERTS FROM GMAIL ===
        The user's bank sends email alerts for every transaction. These are the
        VERIFIED numbers (identical to the dashboard) - use them exactly:

        {format_snapshot_for_ai(snap)}

        IMPORTANT: You have FULL ACCESS to the user's bank transactions above. When they ask about
        spending, income, or their balance - use this REAL data. Reference specific transactions
        when giving advice.
        """
        except Exception as e:
            print(f"Error loading Gmail context: {e}")
            gmail_context = ""
            bank_alerts_context = ""
        
        # 6. Detect if the user wants to create a goal AND has provided all required info
        # Auto-create when user provides item name + amount + deadline (no need to ask again)
        detection_prompt = f"""
        TODAY'S DATE: {current_date}
        CURRENT YEAR: {current_year}
        
        Analyze this conversation and current message:
        
        Recent Conversation:
        {recent_chat_context}
        
        Current Message: "{user_msg}"
        
        === ALL USER'S GOALS AND BILLS ===
        {all_items_context}
        
        Should we CREATE a new goal? Return is_goal: true if BOTH conditions are met:
        1. The user wants to save for something / set a goal (mentioned in conversation OR current message)
        2. ALL required info is now available (item name + amount + deadline) from conversation context
        
        RULES:
        - If user asked to set a goal AND has now provided amount + deadline = CREATE IT (is_goal: true)
        - Example: Conversation mentions "goal for skateboard", current message says "500 dollars by may 2nd" = CREATE IT
        - The item name must come from conversation context, NOT be a pronoun
        - If user says "it" or "the goal", look at conversation to find the actual item name
        - target_amount must be > 0
        - deadline must be a real date - use year {current_year} if no year specified
        - If the date has passed this year, use {current_year + 1}
        - Check if a similar goal already exists - if so, return is_goal: false (no duplicates)
        
        If ALL info available, return: {{"is_goal": true, "item": "Actual Item Name", "target_amount": 500.00, "deadline": "{current_year}-05-02"}}
        If info is missing or goal exists, return: {{"is_goal": false}}
        Return ONLY JSON.
        """
        detect_res = model.generate_content(detection_prompt, generation_config={"response_mime_type": "application/json"})
        goal_data = json.loads(detect_res.text)
        print(f"[v0] Goal detection result: {goal_data}")
        
        if goal_data.get("is_goal"):
            item_name = goal_data.get('item', '')
            target_amount = goal_data.get('target_amount', 0)
            deadline = goal_data.get('deadline', '')
            
            # Validate: Don't create if item is a pronoun or data is missing
            invalid_names = ['it', 'that', 'this', 'the goal', 'goal', 'one', '']
            if item_name.lower().strip() in invalid_names:
                print(f"Rejected goal creation - invalid name: {item_name}")
            elif target_amount <= 0 or not deadline or deadline == 'null':
                print(f"Rejected goal creation - missing data: amount={target_amount}, deadline={deadline}")
            else:
                # Check for duplicates
                is_duplicate = False
                all_existing = goals_collection.get()
                for i in range(len(all_existing['ids'])):
                    existing_item = all_existing['metadatas'][i].get('item', '').lower().strip()
                    if existing_item == item_name.lower().strip():
                        is_duplicate = True
                        break
                
                if not is_duplicate:
                    item_name = to_sentence_case(item_name)
                    goal_id = f"goal_{os.urandom(4).hex()}"
                    print(f"[v0] About to create goal: {item_name}, ${target_amount}, {deadline}, id={goal_id}")
                    goals_collection.add(
                        documents=[f"Goal: Save ${target_amount} for {item_name} by {deadline}"],
                        metadatas=[{"item": item_name, "amount": float(target_amount), "deadline": deadline, "assigned": 0, "category": "goal"}],
                        ids=[goal_id]
                    )
                    print(f"[v0] Goal successfully created: {item_name} - ${target_amount} by {deadline}")
                else:
                    print(f"Rejected goal creation - duplicate exists: {item_name}")

        # 5.5. --- NEW: Backend Auto-Allocation Execution ---
        # If the user is saying "yes" to a proposed allocation, parse and apply it here
        if recent_chat_context != "No previous conversation in this session.":
            try:
                confirm_prompt = f"""
                === CONVERSATION CONTEXT ===
                {recent_chat_context}
                
                === CURRENT MESSAGE ===
                "{user_msg}"
                
                === ALL USER'S GOALS AND BILLS ===
                {all_items_context}
                
                === TASK: DETECT FUND ALLOCATION (PUTTING MONEY TOWARDS A GOAL) ===
                
                IMPORTANT: ALLOCATION means FUNDING an EXISTING goal - adding money towards it.
                This is DIFFERENT from SETTING a goal's target amount.
                
                === WHAT IS ALLOCATION (return is_confirming: true) ===
                - "assign $500 to it" / "fund it with 500" / "allocate 500 to my couch"
                - "put 500 towards my goal" / "add money to it"
                - Confirming a proposed allocation (saying "yes" after AI proposed ALLOCATE: X | $Y)
                - The goal MUST ALREADY EXIST in the list above
                
                === WHAT IS NOT ALLOCATION (return is_confirming: false) ===
                - Setting up a NEW goal: "I want to save $500 for X by Y date"
                - Providing target amount for a goal being created: "215 dollars and I'd like it by april 29th"
                - If the conversation is about CREATING a goal and user provides amount + date = NOT allocation
                - If the goal does NOT exist yet in the list above = NOT allocation
                
                === CRITICAL RULE ===
                If the conversation shows the AI asking "what's the target amount?" or "how much does it cost?"
                and user responds with an amount = This is SETTING THE TARGET, NOT allocating funds.
                Return is_confirming: false in this case.
                
                === PRONOUN RESOLUTION ===
                When user says "it" or "the goal", look at conversation to find which existing goal they mean.
                The item MUST be an EXACT name from the goals list above.
                
                If ALLOCATING to an EXISTING goal, return: {{"is_confirming": true, "allocations": [{{"item": "Exact Goal Name", "amount": 500.00}}]}}
                If NOT allocating (setting up new goal, providing target, etc.), return: {{"is_confirming": false}}
                Return ONLY JSON.
                """
                confirm_res = model.generate_content(confirm_prompt, generation_config={"response_mime_type": "application/json"})
                confirm_data = json.loads(confirm_res.text)
                
                if confirm_data.get("is_confirming") and confirm_data.get("allocations"):
                    updated_items = []
                    all_goals_data = goals_collection.get()
                    
                    for alloc in confirm_data["allocations"]:
                        item_name = alloc.get("item", "").lower().strip()
                        add_amount = float(alloc.get("amount", 0))
                        
                        for i in range(len(all_goals_data['ids'])):
                            meta = all_goals_data['metadatas'][i]
                            db_item = meta.get('item', '').lower().strip()
                            # Flexible matching (e.g., "netflix" matches "Netflix")
                            if db_item == item_name or db_item in item_name or item_name in db_item:
                                goal_id = all_goals_data['ids'][i]
                                new_assigned = float(meta.get('assigned', 0)) + add_amount
                                
                                goals_collection.delete(ids=[goal_id])
                                goals_collection.add(
                                    documents=[all_goals_data['documents'][i]],
                                    metadatas=[{
                                        "item": meta.get('item'),
                                        "amount": meta.get('amount', 0),
                                        "deadline": meta.get('deadline', ''),
                                        "category": meta.get('category', 'goal'),
                                        "assigned": new_assigned
                                    }],
                                    ids=[goal_id]
                                )
                                updated_items.append(f"{meta.get('item')} (${add_amount:.2f})")
                                break
                    
                    if updated_items:
                        # Append system instruction so the AI knows it was successful and confirms to the user
                        user_msg += f"\n\n[SYSTEM NOTE: The system has successfully and automatically allocated funds to: {', '.join(updated_items)}. Tell the user the dashboard is updated and they should refresh the page to see the changes.]"
                        print(f"Backend Auto-Allocated: {updated_items}")
            except Exception as e:
                print(f"Auto-allocation detection failed: {e}")
        
        # 5.6. --- NEW: Backend Goal UPDATE Execution (for setting/changing target amount or deadline) ---
        if recent_chat_context != "No previous conversation in this session.":
            try:
                update_prompt = f"""
                TODAY'S DATE: {current_date}
                CURRENT YEAR: {current_year}
                
                Recent Conversation:
                {recent_chat_context}
                
                Current User Message: "{user_msg}"
                
=== ALL USER'S GOALS AND BILLS ===
                {all_items_context}
                
                Is the user asking to UPDATE an existing goal's TARGET AMOUNT or DEADLINE?
                
                === CONTEXT-AWARE PRONOUN RESOLUTION ===
                When user says "it", "that", "this", "the goal":
                1. Look at the CONVERSATION CONTEXT above
                2. Find what goal/bill was most recently discussed
                3. Use THAT item's EXACT name from the goals/bills list
                
                === INTENT DISAMBIGUATION ===
                "assign" can mean different things - PAY ATTENTION TO CONTEXT:
                - "assign $2000 to it" or "assign 500" (after discussing a goal) = ALLOCATE/FUND - return is_update: false
                - "assign $2000 as the TARGET" or "set target to $2000" or "change the amount to $2000" = UPDATE target - return is_update: true
                - "fund it", "allocate funds", "put money towards" = ALLOCATE/FUND - return is_update: false
                
                Only return is_update: true for SETTING/CHANGING the target amount or deadline, NOT for funding.
                
                === DATE RULES (CRITICAL) ===
                - If user says a date without a year (e.g., "April 12th"), use year {current_year} or later
                - NEVER use a past year like 2024 or 2025 if current year is {current_year}
                - If the date has already passed this year, use {current_year + 1}
                - Always return deadline in YYYY-MM-DD format
                
                === RULES ===
                - goal_name MUST be an EXACT name from the goals/bills list above
                - If user says "it", resolve to the actual goal name from conversation
                - amount = new target amount (0 if not being changed)
                - deadline = new deadline in YYYY-MM-DD (empty if not being changed)
                
                If updating TARGET/DEADLINE, return: {{"is_update": true, "goal_name": "Exact Name From List", "amount": 1850.00, "deadline": "{current_year}-05-23"}}
                If NOT updating (or if user wants to FUND/ALLOCATE), return: {{"is_update": false}}
                Return ONLY JSON.
                """
                update_res = model.generate_content(update_prompt, generation_config={"response_mime_type": "application/json"})
                update_data = json.loads(update_res.text)
                print(f"[v0] Goal update detection: {update_data}")
                
                if update_data.get("is_update"):
                    goal_name = update_data.get("goal_name", "").strip()
                    new_amount = update_data.get("amount", 0)
                    new_deadline = update_data.get("deadline", "")
                    
                    # Find and update the goal
                    all_goals_data = goals_collection.get()
                    goal_updated = False
                    
                    for i in range(len(all_goals_data['ids'])):
                        meta = all_goals_data['metadatas'][i]
                        db_item = meta.get('item', '').lower().strip()
                        
                        # Flexible matching
                        if db_item == goal_name.lower().strip() or goal_name.lower().strip() in db_item or db_item in goal_name.lower().strip():
                            goal_id = all_goals_data['ids'][i]
                            
                            # Build updated metadata
                            updated_meta = {
                                "item": meta.get('item'),
                                "amount": float(new_amount) if new_amount > 0 else float(meta.get('amount', 0)),
                                "deadline": new_deadline if new_deadline else meta.get('deadline', ''),
                                "category": meta.get('category', 'goal'),
                                "assigned": float(meta.get('assigned', 0))
                            }
                            
                            # Delete and re-add with updated data
                            goals_collection.delete(ids=[goal_id])
                            goals_collection.add(
                                documents=[f"Goal: Save ${updated_meta['amount']} for {updated_meta['item']} by {updated_meta['deadline']}"],
                                metadatas=[updated_meta],
                                ids=[goal_id]
                            )
                            
                            goal_updated = True
                            user_msg += f"\n\n[SYSTEM NOTE: Successfully updated {meta.get('item')} - Target: ${updated_meta['amount']}, Deadline: {updated_meta['deadline']}. Tell the user their goal has been updated and they should refresh the Savings Goals page.]"
                            print(f"[v0] Goal updated: {meta.get('item')} -> amount=${updated_meta['amount']}, deadline={updated_meta['deadline']}")
                            break
                    
                    if not goal_updated:
                        # Goal doesn't exist - CREATE it instead of failing
                        if new_amount > 0 and new_deadline:
                            item_name = to_sentence_case(goal_name)
                            goal_id = f"goal_{os.urandom(4).hex()}"
                            goals_collection.add(
                                documents=[f"Goal: Save ${new_amount} for {item_name} by {new_deadline}"],
                                metadatas=[{"item": item_name, "amount": float(new_amount), "deadline": new_deadline, "assigned": 0, "category": "goal"}],
                                ids=[goal_id]
                            )
                            user_msg += f"\n\n[SYSTEM NOTE: Created new goal '{item_name}' with target ${new_amount} by {new_deadline}. Tell the user their goal has been created and they should refresh the Savings Goals page.]"
                            print(f"[v0] Goal created (fallback): {item_name} - ${new_amount} by {new_deadline}")
                        else:
                            print(f"[v0] Goal update failed - could not find goal: {goal_name}")
                        
            except Exception as e:
                print(f"Goal update detection failed: {e}")

        # 6. Final Advisor Response (Grounded in History + Goals + Accounts + Context)
        advisor_prompt = f"""
        You are the Fintech AI Student Financial Advisor.
        {date_context}
        
        === USER'S BANK ACCOUNTS ===
        {accounts_context}
        
        === ALL SAVINGS GOALS ===
        {goals_full_context}
        
        === ALL BILLS ===
        {bills_full_context}
        
        === SPENDING HISTORY (Manual Entries) ===
        {history_context}
        
        {gmail_context}
        
        {bank_alerts_context}
        
        === CONVERSATION HISTORY (READ THIS CAREFULLY) ===
        {recent_chat_context}
        
        Current User Message: {user_msg}
        
        INSTRUCTIONS:
        - You have FULL ACCESS to the user's account balances shown above. When they ask about their balance, tell them!
        - The "Ready to Assign" amount is what they have available after subtracting money already assigned to goals/bills.
        - If the user just set a goal, confirm it and calculate how much they need to save daily/weekly to reach it by their deadline.
        - If they are asking for advice, look at their recent spending to see if they can afford their goals.
        - If user wants to allocate money (e.g., "allocate $2000 to my bills"), show them a breakdown of how you'd distribute it.
          CRITICAL: You MUST format each proposed allocation EXACTLY as a bulleted list using this syntax:
          * ALLOCATE: [Item Name] | $[Amount]
          Then ask: "Shall I update your dashboard with these changes?" - but do NOT actually make changes until they confirm.
        - If the Current User Message includes a [SYSTEM NOTE] saying allocations were successful, warmly inform the user that their dashboard has been updated and they can refresh their Savings Goals page to see it.
        - Keep the tone encouraging but realistic for a student.
        
        ### CONVERSATIONAL CONTEXT & MEMORY (CRITICAL):
        Before responding, ALWAYS scan the ENTIRE conversation history above to understand context:
        
        1. IDENTIFY THE ACTIVE SUBJECT: Track what goal/bill/item is currently being discussed.
           - When user mentions a specific item (e.g., "iPhone 17"), that becomes the ACTIVE SUBJECT
           - The ACTIVE SUBJECT remains until the user explicitly mentions a DIFFERENT item
           
        2. RESOLVE PRONOUNS & REFERENCES: When user says "it", "that", "this", "the goal", etc.:
           - ALWAYS replace with the ACTIVE SUBJECT from conversation history
           - Example: If discussing "iPhone 17" and user says "allocate $2000 to it" -> "allocate $2000 to iPhone 17"
           - NEVER create a goal/bill named "it", "that", "this", or any pronoun
           
        3. LINK RELATED INFORMATION: When user provides amount, date, or allocation in follow-up messages:
           - Connect it to the ACTIVE SUBJECT being discussed
           - Example: User says "I want an iPhone 17" then later "$8900 by May 23rd" -> iPhone 17 costs $8900, deadline May 23rd
           
        4. CHECK FOR DUPLICATES: Before creating ANY new goal:
           - Scan the Active Goals list for similar names (iPhone 17 = iphone 17 = Iphone17 = phone if discussing iPhone)
           - If a similar goal exists, UPDATE it instead of creating a new one
        
        ### GOAL CREATION RULES (MANDATORY):
        When user wants to create a new goal, you MUST collect this information BEFORE creating:
        
        1. REQUIRED - Goal Name: What are they saving for? (Must be specific, not a pronoun)
        2. REQUIRED - Target Date/Deadline: When do they want to reach this goal?
        3. RECOMMENDED - Target Amount: How much do they need?
        
        WORKFLOW:
        - If user provides goal name but NO date: ASK "When would you like to reach this goal?"
        - If user provides goal name but NO amount: ASK "How much does [goal name] cost?" OR offer "Would you like to set the amount later?"
        - If user says "just set it" or "I'll add amount later": You may create with amount=0, but date is STILL required
        - ONLY use the ALLOCATE format AFTER the goal exists and user wants to add money to it
        
        NEVER:
        - Create a goal with a pronoun as the name ("it", "that", "this")
        - Create duplicate goals for the same item
        - Create a goal without asking for the deadline first
        
        ### INVOICE CREATION RULES (MONEY OWED TO THE USER):
        An INVOICE tracks money that someone OWES the user (a client/customer), NOT money the user spent.
        Trigger phrases: "X owes me", "bill X for", "X needs to pay me", "set an invoice", "log an invoice",
        "create an invoice", "X is paying me later", "X will pay on [date]".

        Required fields to create an invoice:
        1. REQUIRED - Client Name: who owes the money (e.g., "Mr James", "Jimmy Ayo")
        2. REQUIRED - Amount: how much is owed (in NGN unless another currency is stated)
        3. OPTIONAL - Due Date: when it should be paid (e.g., "August 5"). Use current/future year only.
        4. OPTIONAL - Description: what it is for. Extract from phrasing like "owes me 15000 FOR groceries"
           -> description = "groceries". If no description is given, leave it blank and still create the invoice.

        WORKFLOW (CRITICAL):
        - If the user states an owed amount WITHOUT explicitly asking to set an invoice
          (e.g., "Mr James owes me 15,000 due August 5"), DO NOT create it immediately.
          Instead ASK: "Would you like me to set an invoice for this?" and wait for confirmation.
        - If the user explicitly says to set/create/log the invoice in the same message
          (e.g., "...set an invoice for it"), create it right away (no extra confirmation needed).
        - To ACTUALLY create the invoice, you MUST output a marker line in EXACTLY this format on its own line:
          * CREATE_INVOICE: [Client Name] | [Amount] | [Due Date or NONE] | [Description or NONE]
          Example: * CREATE_INVOICE: Mr James | 15000 | 2026-08-05 | groceries
          Example: * CREATE_INVOICE: Jimmy Ayo | 50000 | NONE | NONE
        - Only output the CREATE_INVOICE marker when you intend the invoice to be saved (either the user
          explicitly asked, OR they just confirmed "yes" to your "Would you like me to set an invoice?" question).
        - When you output the marker, also write a friendly confirmation sentence like
          "Done - I've logged an invoice for Mr James (₦15,000) due August 5."
        - Dates in the marker MUST be in YYYY-MM-DD format. If no date was given, use NONE.

        ### INTENT DISAMBIGUATION - "ASSIGN" AND SIMILAR WORDS:
        The word "assign" can mean different things - understand the user's TRUE intent:
        ALLOCATE/FUND (add money TOWARDS a goal - increases "assigned" amount):
        - "assign $2000 to it" / "assign 2000 dollars to my goal"
        - "fund it with $500" / "allocate $1000 towards my car"
        - "put $300 towards my vacation" / "add money to my savings"
        - Use the ALLOCATE format: * ALLOCATE: [Goal Name] | $[Amount]
        
        SET TARGET (change the goal's TARGET amount):
        - "assign $2000 AS the target" / "set the target to $2000"
        - "change the amount to $500" / "update target amount"
        - "the goal should be $3000" / "make it a $5000 goal"
        - This changes the goal's target_amount field
        
        SET DEADLINE (change the goal's deadline):
        - "assign a date of May 23rd" / "set the deadline to June 1st"
        - "change the date to April 15th" / "update the deadline"
        - This changes the goal's deadline field
        
        When ambiguous, consider context:
        - If goal already has a target and user says "assign $X to it" -> likely ALLOCATE (funding)
        - If goal has NO target ($0) and user says "assign $X to it" -> could be setting target, ASK to clarify
        
        ### DATE & TIME CALCULATIONS (CRITICAL - USE EXACT VALUES):
        - ALWAYS use the pre-calculated date values provided above. NEVER estimate or guess dates.
        - When user mentions a date WITHOUT a year, ALWAYS use the current year or a future year. NEVER use a past year.
        - When calculating days until a deadline:
          1. Use the "Days remaining in [month]" value from above for the current month
          2. Add full days for any complete months in between
          3. Add the day number for the target month
          Example: If today is April 8 and deadline is May 15:
            - Days left in April: Use the exact value above (NOT an estimate)
            - Days in May until 15th: 15
            - Total = Days left in April + 15
        - For daily/weekly savings: Divide the target amount by the EXACT number of days calculated
        - Month lengths: Jan=31, Feb=28(29 leap), Mar=31, Apr=30, May=31, Jun=30, Jul=31, Aug=31, Sep=30, Oct=31, Nov=30, Dec=31
        
        ### INFORMATION FILTERING & SCOPE (CRITICAL):
        Always analyze the user's intent to determine what information to show:
        
        1. IDENTIFY THE SUBJECT: Determine the specific financial noun the user is asking about.
           Examples: "Starbucks", "bills", "goals", "rent", "groceries", "Zenith Bank account"
        
        2. MATCH & FILTER based on specificity level:
           - SPECIFIC (e.g., "Show my Starbucks spending", "What about my Netflix bill?"):
             Filter to ONLY show data matching that specific item/merchant/name.
           - CATEGORICAL (e.g., "Show my goals", "What are my bills?", "Food expenses"):
             Filter to ONLY show items matching that category. 
             * "goals" = items with Category: goal (savings targets like new phone, vacation)
             * "bills" = items with Category: bill (recurring payments like Netflix, Rent)
             * Spending categories = filter transactions by that category keyword
           - BROAD (e.g., "How's my budget?", "Show everything", "Financial summary"):
             Provide a high-level summary across ALL relevant categories.
        
        3. LABEL CLEARLY: When showing multiple types of data, use clear headers:
           - "Your Bills:" for bill items
           - "Your Savings Goals:" for goal items  
           - "Recent Transactions:" for spending history
           - "Account Balances:" for account info
           This prevents confusion when displaying mixed data types.
        """

        if channel == "whatsapp":
            advisor_prompt += """

        ### WHATSAPP MESSAGE FORMATTING (THIS REPLY IS SENT AS A PLAIN WHATSAPP TEXT MESSAGE):
        - NEVER use the asterisk character (*) anywhere in your reply - no markdown bold (*text*), no
          asterisk bullet points, no asterisk emphasis of any kind. A stray asterisk renders literally on
          WhatsApp and looks broken.
        - Use UPPERCASE for section headers and key labels instead of bold syntax, e.g. MERCHANT:,
          TOTAL OUTFLOW:, FINANCIAL SNAPSHOT, CATEGORY:, BALANCE:.
        - For any list of transactions or items, use the bullet "▪️" - never *, -, or numbers.
        - Only use these subtle, professional emojis, and only as section header markers: 💳 📊 🏛️ ▪️.
          Never use bright or casual emojis (no 🍕 🛒 🥳 😀, etc).
        - Keep details grouped tightly with no blank line between a header and the lines beneath it.
          Leave exactly ONE blank line between major sections.
        - End the response with exactly one short footer line, italicized with single underscores,
          e.g. _Loamy - your financial companion_
        - Do not use any other markdown (#, backticks, double underscores, tildes, etc).
        """

        response = model.generate_content(advisor_prompt)
        reply_text = response.text

        # --- Handle CREATE_INVOICE marker (AI-driven invoice creation) ---
        # The AI emits a line like: * CREATE_INVOICE: Mr James | 15000 | 2026-08-05 | groceries
        invoice_created = False
        try:
            import re
            from datetime import datetime as _dt
            import uuid as _uuid

            cleaned_lines = []
            for line in reply_text.split("\n"):
                marker_match = re.search(r"CREATE_INVOICE:\s*(.+)", line)
                if marker_match:
                    parts = [p.strip() for p in marker_match.group(1).split("|")]
                    if len(parts) >= 2 and parts[0] and parts[1]:
                        client_name = parts[0]
                        # Extract numeric amount
                        amount_raw = re.sub(r"[^\d.]", "", parts[1])
                        amount = float(amount_raw) if amount_raw else 0
                        due_date = ""
                        description = ""
                        if len(parts) >= 3 and parts[2].upper() != "NONE":
                            due_date = parts[2]
                        if len(parts) >= 4 and parts[3].upper() != "NONE":
                            description = parts[3]

                        if amount > 0:
                            invoice_id = f"inv_{_uuid.uuid4().hex[:12]}"
                            created_date = _dt.now().strftime("%Y-%m-%d")
                            invoices_collection.add(
                                ids=[invoice_id],
                                documents=[f"Invoice for {client_name}: {description} - {amount} NGN"],
                                metadatas=[{
                                    "user_id": user_id,
                                    "client_name": client_name,
                                    "amount": amount,
                                    "description": description,
                                    "due_date": due_date,
                                    "category": "Invoice",
                                    "status": "unpaid",
                                    "created_date": created_date,
                                    "paid_date": ""
                                }]
                            )
                            invoice_created = True
                            print(f"[INVOICE] Created from chat: {client_name} - {amount} NGN due {due_date}")
                    # Skip the marker line so it isn't shown to the user
                    continue
                cleaned_lines.append(line)

            reply_text = "\n".join(cleaned_lines).strip()
        except Exception as e:
            print(f"[INVOICE] Error parsing CREATE_INVOICE marker: {e}")

        return {"reply": reply_text, "invoice_created": invoice_created}
        
    except Exception as e:
        import traceback
        print(f"Chat Error: {str(e)}")
        print(f"Full traceback: {traceback.format_exc()}")
        return {"reply": f"I'm having trouble accessing your financial profile right now. Debug info: {str(e)}"}


# --- WhatsApp AI bridge -------------------------------------------------------
# The ONE seam between the WhatsApp transport and the AI brain. whatsapp.py
# stays pure transport (it only knows a phone number); this bridge does the
# identity resolution and hands the AI a real, Supabase-backed user_id. Kept
# small and single-purpose (Logic/UI Separation + Atomic Modularity rules).
import database  # data-access layer; the only module that talks to Supabase
import chat_service  # builds the combined Gmail + ledger snapshot for all channels


async def _whatsapp_ai_handler(text: str, from_phone: str) -> str:
    # A. Resolve the WhatsApp phone to a Loamy account (Phase 1: look up an
    #    existing user by their saved phone_number; full self-service linking
    #    comes in a later phase). DB work is blocking, so off-load to a thread.
    try:
        lookup = await asyncio.to_thread(database.get_user_by_phone, from_phone)
    except Exception as e:
        print(f"[WhatsApp bridge] Could not resolve user for {from_phone}: {e}")
        return "I couldn't reach your account right now. Please try again shortly."

    user_row = lookup.get("data") if lookup.get("status") == "success" else None
    if not user_row:
        return ("I couldn't find a Loamy account linked to this number yet. "
                "Open the Loamy web app, go to your Dashboard, and use "
                "\"Connect WhatsApp\" to link this number - then message me again.")
    user_id = user_row["user_id"]

    # C. Give the advisor the SAME snapshot the web chat and dashboard use, from
    #    the shared compute_bank_snapshot() so every channel reports identical
    #    numbers. (Previously WhatsApp read an empty Supabase table -> ₦0.00.)
    snap = await asyncio.to_thread(compute_bank_snapshot, user_id)
    if snap.get("alert_count", 0) > 0:
        text = (
            "=== VERIFIED FINANCIAL SNAPSHOT (use these exact numbers) ===\n"
            f"{format_snapshot_for_ai(snap)}\n"
            "=== END SNAPSHOT ===\n\n"
        ) + text

    result = await chat_with_history({"text": text, "user_id": user_id, "channel": "whatsapp"})
    return result.get("reply", "")


set_ai_handler(_whatsapp_ai_handler)


async def _whatsapp_receipt_handler(file_data: bytes, mime_type: str, from_phone: str) -> str:
    """Turn a receipt photo sent over WhatsApp into a saved expense.

    Same identity path as the text handler (phone -> Supabase user), then reuses
    the SHARED process_receipt_image() pipeline so a WhatsApp photo and a web
    upload land as identical dashboard entries. Returns the confirmation text
    whatsapp.py sends back to the user."""
    try:
        lookup = await asyncio.to_thread(database.get_user_by_phone, from_phone)
    except Exception as e:
        print(f"[WhatsApp receipt] user lookup failed for {from_phone}: {e}")
        return "I couldn't reach your account right now. Please try again shortly."

    user_row = lookup.get("data") if lookup.get("status") == "success" else None
    if not user_row:
        return ("I couldn't find a Loamy account linked to this number yet. "
                "Open the Loamy web app, go to your Dashboard, and use "
                "\"Connect WhatsApp\" to link this number - then resend your receipt.")
    user_id = user_row["user_id"]

    result = await asyncio.to_thread(process_receipt_image, file_data, mime_type, user_id)

    if not result or result.get("error"):
        return "I couldn't read that receipt clearly. Please try a sharper, well-lit photo."

    analysis = result.get("analysis", {}) or {}
    vendor = (analysis.get("vendor") or "the vendor").upper()
    category = (analysis.get("category") or "Uncategorized").upper()
    cur = result.get("currency_info", {}) or {}
    ngn = cur.get("displayed_amount")

    if ngn is not None:
        try:
            amount_str = f"\u20a6{float(ngn):,.2f}"
        except (TypeError, ValueError):
            amount_str = "the amount"
        if cur.get("is_estimate"):
            amount_str += " (estimated)"
    else:
        amount_str = "the amount"

    # Matches the WhatsApp house style: uppercase labels, no asterisks, plain
    # square bullet, italic footer (see the WHATSAPP MESSAGE FORMATTING rules
    # in chat_with_history for the same convention on chat replies).
    return (
        f"💳 RECEIPT LOGGED\n"
        f"MERCHANT: {vendor}\n"
        f"AMOUNT: {amount_str}\n"
        f"CATEGORY: {category}\n"
        f"\n"
        f"_Synced to your Loamy dashboard_"
    )


set_receipt_handler(_whatsapp_receipt_handler)


@app.get("/get-goals")
async def get_goals(user_id: str = "default"):
    """Return this user's goals (Supabase-backed), deduped by item name."""
    try:
        res = database.get_goals(user_id)
        rows = res["data"] if res["status"] == "success" else []
        goals = []
        seen_items = {}  # Track items to dedupe in response

        for row in rows:
            item = row.get("item") or ""
            key = item.lower()
            # Only include first occurrence of each item
            if key in seen_items:
                continue
            seen_items[key] = True

            goals.append({
                "id": row.get("id"),
                "item": item,
                "amount": row.get("amount", 0),
                "deadline": row.get("deadline", ""),
                "category": row.get("category", "goal"),
                "assigned": row.get("assigned", 0),
            })
        return {"goals": goals}
    except Exception as e:
        return {"goals": [], "error": str(e)}


# ========== CURRENCY CONVERSION ENDPOINTS ==========

@app.get("/get-exchange-rate")
async def get_exchange_rate_endpoint(from_currency: str = "USD", to_currency: str = "NGN"):
    """
    Get current exchange rate between two currencies.
    Uses free ExchangeRate-API with 1-hour caching.
    """
    try:
        rate = get_exchange_rate(from_currency.upper(), to_currency.upper())
        return {
            "status": "success",
            "from_currency": from_currency.upper(),
            "to_currency": to_currency.upper(),
            "rate": rate,
            "example": f"1 {from_currency.upper()} = {rate} {to_currency.upper()}"
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/convert-currency")
async def convert_currency_endpoint(data: dict):
    """
    Convert an amount from one currency to another.
    """
    try:
        amount = float(data.get("amount", 0))
        from_currency = data.get("from_currency", "USD").upper()
        to_currency = data.get("to_currency", "NGN").upper()
        
        result = convert_to_local_currency(amount, from_currency, to_currency)
        return {"status": "success", **result}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/get-pending-conversions")
async def get_pending_conversions():
    """
    Get list of foreign currency transactions awaiting bank true-up.
    """
    try:
        results = pending_conversions_collection.get(where={"status": "pending"})
        pending = []
        for i in range(len(results['ids'])):
            pending.append({
                "id": results['ids'][i],
                **results['metadatas'][i]
            })
        return {"status": "success", "pending_conversions": pending, "count": len(pending)}
    except Exception as e:
        return {"status": "error", "error": str(e), "pending_conversions": []}


# ============================================
# DATA CLEANUP ENDPOINTS - Remove Bad/Hallucinated Data
# ============================================

@app.get("/list-all-transactions")
async def list_all_transactions():
    """
    List all transactions in the vault for review before cleanup.
    Shows ID, vendor, amount, date so user can identify bad records.
    """
    try:
        all_data = collection.get()
        transactions = []
        for i in range(len(all_data['ids'])):
            meta = all_data['metadatas'][i]
            transactions.append({
                "id": all_data['ids'][i],
                "vendor": meta.get('vendor', 'Unknown'),
                "amount": meta.get('total', 0),
                "currency": meta.get('currency', 'NGN'),
                "date": meta.get('date', ''),
                "category": meta.get('category', ''),
                "document": all_data['documents'][i][:100] if all_data['documents'][i] else ""
            })
        
        # Sort by amount descending to easily spot outliers
        transactions.sort(key=lambda x: float(x.get('amount', 0)), reverse=True)
        
        return {
            "status": "success",
            "count": len(transactions),
            "transactions": transactions
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.delete("/delete-transaction/{transaction_id}")
async def delete_transaction(transaction_id: str):
    """
    Delete a specific transaction by ID.
    Use this to remove hallucinated/bad records.
    """
    try:
        collection.delete(ids=[transaction_id])
        return {"status": "success", "message": f"Deleted transaction {transaction_id}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/cleanup-bad-data")
async def cleanup_bad_data():
    """
    Automatically clean up known bad data patterns:
    1. Amounts over 100 million NGN (likely hallucinated)
    2. Transactions from marketing senders (Codecademy, Discord, etc.)
    3. Amounts under 50 NGN from non-bank sources (likely fake)
    
    Returns list of deleted items for review.
    """
    deleted_items = []
    
    try:
        all_data = collection.get()
        ids_to_delete = []
        
        # Known marketing/promotional senders that should never have transactions
        marketing_senders = [
            "codecademy", "discord", "dribbble", "artgrid", "linkedin",
            "coursera", "udemy", "skillshare", "newsletter", "promo",
            "twitter", "facebook", "instagram", "github", "notion"
        ]
        
        for i in range(len(all_data['ids'])):
            meta = all_data['metadatas'][i]
            doc = all_data['documents'][i].lower() if all_data['documents'][i] else ""
            amount = float(meta.get('total', 0))
            vendor = meta.get('vendor', '').lower()
            
            should_delete = False
            reason = ""
            
            # Rule 1: Amounts over 100 million NGN are hallucinated
            if amount > 100_000_000:
                should_delete = True
                reason = f"Amount too large: ₦{amount:,.2f} (likely hallucinated)"
            
            # Rule 2: Marketing sender names
            for sender in marketing_senders:
                if sender in vendor or sender in doc:
                    should_delete = True
                    reason = f"Marketing sender detected: {vendor}"
                    break
            
            # Rule 3: Suspiciously small amounts (under ₦50) that aren't from banks
            if amount > 0 and amount < 50 and "bank" not in vendor.lower():
                should_delete = True
                reason = f"Suspiciously small amount: ��{amount:.2f} from {vendor}"
            
            if should_delete:
                ids_to_delete.append(all_data['ids'][i])
                deleted_items.append({
                    "id": all_data['ids'][i],
                    "vendor": meta.get('vendor'),
                    "amount": amount,
                    "reason": reason
                })
        
        # Delete all bad records
        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
        
        return {
            "status": "success",
            "deleted_count": len(deleted_items),
            "deleted_items": deleted_items
        }
        
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/cleanup-gmail-data")
async def cleanup_gmail_data():
    """
    Clean up bad Gmail-extracted data:
    1. Emails from marketing senders
    2. Hallucinated amounts
    3. Non-financial emails that were incorrectly classified
    """
    deleted_items = []
    
    try:
        all_data = gmail_data_collection.get()
        ids_to_delete = []
        
        marketing_blacklist = [
            "codecademy", "discord", "dribbble", "artgrid", "linkedin",
            "coursera", "udemy", "skillshare", "newsletter", "promo",
            "twitter", "facebook", "instagram", "github", "notion",
            "substack", "medium", "mailchimp"
        ]
        
        for i in range(len(all_data['ids'])):
            meta = all_data['metadatas'][i]
            doc = all_data['documents'][i].lower() if all_data['documents'][i] else ""
            amount = float(meta.get('amount', 0)) if meta.get('amount') else 0
            sender = meta.get('sender_email', '').lower()
            
            should_delete = False
            reason = ""
            
            # Rule 1: Marketing senders
            for blacklisted in marketing_blacklist:
                if blacklisted in sender or blacklisted in doc:
                    should_delete = True
                    reason = f"Marketing sender: {sender}"
                    break
            
            # Rule 2: Amounts over 100 million (hallucinated)
            if amount > 100_000_000:
                should_delete = True
                reason = f"Hallucinated amount: ₦{amount:,.2f}"
            
            # Rule 3: Very small amounts from non-banks
            if amount > 0 and amount < 50:
                is_bank = any(b in sender for b in ['bank', 'alat', 'gtb', 'zenith', 'firstbank', 'access', 'uba'])
                if not is_bank:
                    should_delete = True
                    reason = f"Small non-bank amount: ₦{amount:.2f}"
            
            if should_delete:
                ids_to_delete.append(all_data['ids'][i])
                deleted_items.append({
                    "id": all_data['ids'][i],
                    "sender": sender,
                    "amount": amount,
                    "reason": reason
                })
        
        if ids_to_delete:
            gmail_data_collection.delete(ids=ids_to_delete)
        
        return {
            "status": "success",
            "deleted_count": len(deleted_items),
            "deleted_items": deleted_items
        }
        
    except Exception as e:
        return {"status": "error", "error": str(e)}
        return {"status": "success", "pending_conversions": pending, "count": len(pending)}
    except Exception as e:
        return {"status": "error", "error": str(e), "pending_conversions": []}


# ========== SHARED FINANCIAL SNAPSHOT (single source of truth) ==========

def load_user_emails(user_id: str) -> list:
    """Load a user's synced emails, Supabase-primary with ChromaDB fallback.

    Supabase `gmail_data` is now the source of truth. If it has no row yet
    (e.g. before the first sync after this change), we fall back to the local
    ChromaDB blob so nothing breaks in the meantime - the next /sync-gmail-data
    mirrors into Supabase and this fallback stops being needed.
    """
    # 1) Supabase first.
    try:
        res = database.get_gmail_data(user_id)
        if res["status"] == "success" and res["data"]:
            emails = res["data"].get("emails") or []
            if emails:
                return emails
    except Exception as e:
        print(f"[v0] load_user_emails Supabase read failed: {e}")

    # 2) Fallback: local ChromaDB blob.
    try:
        chroma = gmail_data_collection.get(ids=[f"gmail_{user_id}"])
        parsed = []
        for i in range(len(chroma.get('ids', []))):
            doc = chroma['documents'][i]
            if not doc:
                continue
            try:
                parsed.extend(json.loads(doc).get("emails", []))
            except Exception:
                continue
        return parsed
    except Exception as e:
        print(f"[v0] load_user_emails ChromaDB fallback failed: {e}")
        return []


def compute_bank_snapshot(user_id: str) -> dict:
    """The ONE balance/cash-flow computation shared by every chat surface.

    Reads the per-user Gmail blob from `gmail_data_collection` (the same store
    the dashboard reads) and applies the SAME rules the dashboard uses, so the
    web chat, WhatsApp, and the dashboard all report identical numbers:
      - balance = sum of each bank's CHRONOLOGICALLY LATEST alert balance,
        compared by (internal_date_ms, date) so same-day alerts resolve correctly
        (this is what fixes the web chat's "first alert wins" bug).
      - total_credits / total_debits summed across all alerts.
      - spending grouped by normalized category.
    """
    snap = {
        "balance": 0.0, "bank_name": None, "last_date": None,
        "total_credits": 0.0, "total_debits": 0.0,
        "spending_by_category": {}, "recent_transactions": [], "alert_count": 0,
    }
    try:
        parsed = load_user_emails(user_id)

        bank_balances = {}
        alerts = []
        for email in parsed:
            if not (email.get('is_bank_alert') is True or email.get('is_bank_alert') == 'true'):
                continue
            bank = email.get('bank_name') or 'Bank'
            balance = float(email.get('balance', 0) or 0)
            amount = float(email.get('amount', 0) or 0)
            date = email.get('date', '') or ''
            internal_ms = int(email.get('internal_date_ms', 0) or 0)
            tx_type = (email.get('transaction_type', '') or 'debit')
            narration = email.get('narration') or email.get('subject') or 'Transaction'
            category = email.get('category', '') or ''

            if balance > 0:
                existing = bank_balances.get(bank)
                key = (internal_ms, date)
                if existing is None or key > (existing['internal_ms'], existing['date']):
                    bank_balances[bank] = {'balance': balance, 'date': date, 'internal_ms': internal_ms}

            if amount > 0:
                alerts.append({
                    'date': date, 'internal_ms': internal_ms, 'bank': bank,
                    'amount': amount, 'type': tx_type, 'narration': narration,
                })
                if tx_type == 'credit':
                    snap['total_credits'] += amount
                else:
                    snap['total_debits'] += amount
                    cat = normalize_category(category or 'Other') or 'Other'
                    snap['spending_by_category'][cat] = snap['spending_by_category'].get(cat, 0) + amount

        snap['balance'] = round(sum(b['balance'] for b in bank_balances.values()), 2)
        if bank_balances:
            latest = max(bank_balances.items(), key=lambda kv: (kv[1]['internal_ms'], kv[1]['date']))
            snap['bank_name'] = latest[0]
            snap['last_date'] = latest[1]['date']
        snap['total_credits'] = round(snap['total_credits'], 2)
        snap['total_debits'] = round(snap['total_debits'], 2)
        snap['spending_by_category'] = {k: round(v, 2) for k, v in snap['spending_by_category'].items()}

        alerts.sort(key=lambda a: (a['internal_ms'], a['date']), reverse=True)
        snap['recent_transactions'] = alerts[:30]
        snap['alert_count'] = len(alerts)
    except Exception as e:
        print(f"[v0] compute_bank_snapshot error: {e}")
    return snap


def format_snapshot_for_ai(snap: dict) -> str:
    """Render the shared snapshot as prompt-ready text for Gemini."""
    cats = "\n".join(
        f"  - {c}: ₦{a:,.2f}" for c, a in snap.get("spending_by_category", {}).items()
    ) or "  - No categorized spending yet."
    txns = "\n".join(
        f"  - {t['date']}: {t['bank']} {t['type'].upper()} ₦{t['amount']:,.2f} | {t['narration'][:40]}"
        for t in snap.get("recent_transactions", [])[:15]
    ) or "  - No bank transactions yet."
    bank_str = f" (latest alert from {snap['bank_name']} on {snap['last_date']})" if snap.get("bank_name") else ""
    return f"""- Current Balance: ₦{snap.get('balance', 0):,.2f}{bank_str}
- Total Money In: ₦{snap.get('total_credits', 0):,.2f}
- Total Money Out: ₦{snap.get('total_debits', 0):,.2f}
- Bank alerts on file: {snap.get('alert_count', 0)}

=== SPENDING BY CATEGORY ===
{cats}

=== RECENT BANK TRANSACTIONS (most recent first) ===
{txns}"""


# ========== DASHBOARD ENDPOINT ==========

@app.get("/get-dashboard-data")
async def get_dashboard_data(user_id: str = "default"):
    """
    Aggregates all financial data for the dashboard:
    - Bank accounts and balances (from Gmail sync)
    - Cash flow (income/expenses)
    - Expense breakdown by category
    - Recent transactions (bank alerts + receipts)
    - Goals progress
    - Needs review items (uncategorized transfers)
    - Financial summary
    """
    try:
        # 0. Auto-refresh bank data server-side (throttled), for THIS user only.
        #    This is what makes the dashboard update on its own without the user
        #    reconnecting Gmail. It used to call maybe_autosync_all_users(), which
        #    synced every user with stored Gmail credentials before returning a
        #    single dashboard - so user #5's load waited on users #1-4's Gmail
        #    syncs. Scoping to user_id and firing it in the background (instead of
        #    awaiting it) means this response is never blocked by a mail fetch;
        #    the sync just updates storage for the *next* load to pick up.
        try:
            spawn_background_sync(user_id)
        except Exception as _e:
            print(f"[v0] Dashboard: server-side autosync skipped: {_e}")

        # 1. Get bank accounts from synced Gmail data
        accounts = []
        total_balance = 0
        bank_transactions = []  # Track ALL bank transactions for dashboard
        needs_review = []  # Transfers without clear category

        # --- Review Queue bridge ---------------------------------------------
        # Single source of truth for "what has the user already categorized?".
        # The dashboard reads raw bank emails, but the user logs categories from
        # the Review Queue (review_queue_collection). We build a map keyed by the
        # transaction's source_id so resolved items (a) drop out of Needs Review
        # and (b) feed their chosen category into the expense breakdown.
        review_map = {}  # { source_id: {"status": ..., "category": ...} }
        try:
            rq = review_queue_collection.get(where={"user_id": user_id})
            for i in range(len(rq["ids"])):
                rmeta = rq["metadatas"][i] or {}
                sid = rmeta.get("source_id")
                if not sid:
                    continue
                review_map[sid] = {
                    "status": rmeta.get("status", "pending"),
                    "category": rmeta.get("category", "") or "",
                }
        except Exception as e:
            print(f"[v0] Dashboard: could not load review queue state: {e}")

        try:
            # Scope Gmail data to THIS user only, via the shared loader
            # (Supabase-primary with ChromaDB fallback) so the dashboard, chat,
            # and WhatsApp all read the exact same per-user email set.
            bank_balances = {}  # Track latest balance per bank
            parsed_emails = load_user_emails(user_id)

            print(f"[v0] Dashboard: Parsed {len(parsed_emails)} emails for {user_id}")

            for email in parsed_emails:
                is_bank = email.get('is_bank_alert') is True or email.get('is_bank_alert') == 'true'

                if is_bank:
                    bank_name = email.get('bank_name') or 'Unknown Bank'
                    balance = float(email.get('balance', 0) or 0)
                    amount = float(email.get('amount', 0) or 0)
                    date = email.get('date', '')
                    # Precise received time (epoch ms) for sub-day ordering.
                    internal_ms = int(email.get('internal_date_ms', 0) or 0)
                    tx_type = email.get('transaction_type', '')
                    narration = email.get('narration') or email.get('subject') or 'Transaction'
                    category = email.get('category', '')

                    # Keep the balance from the CHRONOLOGICALLY LATEST alert for
                    # each bank. We compare by (internal_date_ms, date) so that
                    # when several alerts share the same calendar day, the one
                    # that actually arrived last wins - this is what makes the
                    # "Current Balance" reflect the true latest balance instead of
                    # whichever same-day email happened to be parsed first.
                    if balance > 0:
                        existing = bank_balances.get(bank_name)
                        this_key = (internal_ms, date)
                        if existing is None or this_key > (existing.get('internal_ms', 0), existing.get('date', '')):
                            bank_balances[bank_name] = {
                                'balance': balance,
                                'date': date,
                                'internal_ms': internal_ms,
                                'currency': email.get('currency', 'NGN')
                            }

                    # Add ALL bank transactions (both credit and debit)
                    if amount > 0:
                        source_id = email.get('id') or f"{narration[:20]}_{amount}"

                        # If the user already categorized this transaction in the
                        # Review Queue, that category wins over the raw email one.
                        review_state = review_map.get(source_id)
                        if review_state and review_state.get("category"):
                            category = review_state["category"]

                        tx_record = {
                            'id': source_id,
                            'description': narration[:50] if narration else 'Bank Transaction',
                            'amount': amount,
                            'type': tx_type or 'debit',
                            'date': date,
                            'internal_date_ms': internal_ms,
                            'bank': bank_name,
                            'category': category,
                            'source': 'bank_alert'
                        }
                        bank_transactions.append(tx_record)

                        # Flag ANY uncategorized bank transaction (credit OR debit) for review.
                        # We deliberately do NOT use receipts here - receipts are already
                        # descriptive. Bank credits/debits are the ones that need clarifying.
                        narration_lower = (narration or '').lower()
                        # "Banking" is the placeholder category on every raw bank alert.
                        has_no_category = not category or category.lower() in ['other', 'uncategorized', 'banking', '']
                        # Skip pure bank fees/charges - those don't need user clarification
                        # 'fip' is a NIBSS transfer-reference prefix, NOT a fee - do not filter it.
                        is_not_fee = all(kw not in narration_lower for kw in ['charge', 'fee', 'stamp duty', 'vat', 'levy', 'commission', 'cot', 'maintenance'])
                        this_type = tx_type or 'debit'

                        # Already handled in the Review Queue (resolved or dismissed)?
                        # If so, it must NOT reappear under "Needs Review".
                        already_handled = bool(review_state and review_state.get("status") in ("resolved", "dismissed"))

                        if has_no_category and is_not_fee and not already_handled:
                            needs_review.append({
                                'id': source_id,
                                'description': narration[:50] if narration else 'Transaction',
                                'amount': amount,
                                'date': date,
                                'bank': bank_name,
                                'type': this_type,
                                'suggested_categories': ['Sales', 'Groceries', 'Food', 'Transport', 'Bills', 'Salaries', 'Stock', 'Shopping', 'Other']
                            })

                            # Layer 2: auto-create a chat nudge in the Review Queue for
                            # unrecognized vendors (skip if already known or already queued).
                            try:
                                vendor_name = _extract_vendor_name(narration)
                                vendor_key = _normalize_vendor(vendor_name)
                                known = vendor_memory_collection.get(where={"vendor_key": vendor_key})
                                already = review_queue_collection.get(where={"source_id": source_id})
                                if not known["ids"] and not already["ids"]:
                                    review_queue_collection.add(
                                        ids=[f"rev_{source_id}"],
                                        documents=[f"Review: {vendor_name} {amount}"],
                                        metadatas=[{
                                            "user_id": user_id,
                                            "vendor": vendor_name,
                                            "amount": amount,
                                            "transaction_type": this_type,
                                            "date": date,
                                            "source_id": source_id,
                                            "status": "pending",
                                            "nudge": _build_nudge_message("there", vendor_name, amount, this_type),
                                            "user_reply": "",
                                            "category": "",
                                            "created_at": date or ""
                                        }]
                                    )
                            except Exception as _qe:
                                print(f"[REVIEW] auto-queue error: {_qe}")
            
            for bank_name, data in bank_balances.items():
                accounts.append({
                    'name': bank_name,
                    'balance': data['balance'],
                    'currency': data['currency'],
                    'last_updated': data['date']
                })
                total_balance += data['balance']
                # [v0] DEBUG: surface which alert each bank's balance came from.
                print(
                    f"[v0] Balance pick -> {bank_name}: balance={data['balance']} "
                    f"date={data.get('date')} internal_ms={data.get('internal_ms')}"
                )

            print(f"[v0] Dashboard: Found {len(bank_transactions)} bank transactions, {len(needs_review)} need review")
            print(f"[v0] Dashboard: Computed current_balance (total) = {total_balance}")
        except Exception as e:
            print(f"Error getting bank accounts: {e}")
        
        # 2. Calculate cash flow from bank transactions
        cash_in = 0
        cash_out = 0
        recent_transactions = []
        expense_categories = {}
        
        # Process bank transactions for cash flow
        for tx in bank_transactions:
            amount = tx['amount']
            tx_type = tx['type']
            category = normalize_category(tx.get('category', 'Other') or 'Other')
            
            if tx_type == 'credit':
                cash_in += amount
            elif tx_type == 'debit':
                cash_out += amount
                # Track expense categories for debits
                if category not in expense_categories:
                    expense_categories[category] = 0
                expense_categories[category] += amount
            
            # Add to recent transactions
            recent_transactions.append(tx)
        
        # 3. Get expenses from manual entries
        try:
            expense_results = expenses_collection.get()
            for i in range(len(expense_results['ids'])):
                meta = expense_results['metadatas'][i]
                amount = float(meta.get('amount', 0))
                category = normalize_category(meta.get('category', 'Other'))
                cash_out += amount
                
                if category not in expense_categories:
                    expense_categories[category] = 0
                expense_categories[category] += amount
        except Exception as e:
            print(f"Error getting manual expenses: {e}")
        
        # 4. Get expenses from uploaded receipts (collection)
        try:
            receipt_results = collection.get()
            for i in range(len(receipt_results['ids'])):
                meta = receipt_results['metadatas'][i]
                amount = float(meta.get('total', 0))
                category = normalize_category(meta.get('category', 'Other'))
                date = meta.get('date', '')
                vendor = meta.get('vendor', 'Unknown')
                original_currency = meta.get('original_currency', meta.get('currency', 'NGN'))
                
                # Convert to NGN if foreign currency
                if original_currency != 'NGN':
                    exchange_rate = get_exchange_rate(original_currency, "NGN")
                    amount_ngn = amount * exchange_rate
                else:
                    amount_ngn = amount
                
                cash_out += amount_ngn
                
                if category not in expense_categories:
                    expense_categories[category] = 0
                expense_categories[category] += amount_ngn
                
                # Add to recent transactions
                if len(recent_transactions) < 15:
                    recent_transactions.append({
                        'id': receipt_results['ids'][i],
                        'description': vendor,
                        'amount': amount_ngn,
                        'original_amount': amount,
                        'original_currency': original_currency,
                        'type': 'debit',
                        'date': date,
                        'bank': 'Receipt',
                        'category': category
                    })
        except Exception as e:
            print(f"Error getting receipts: {e}")
        
        # Sort recent transactions by precise time first (internal_date_ms),
        # falling back to the date string, so same-day items keep true order.
        recent_transactions.sort(key=lambda x: (x.get('internal_date_ms', 0) or 0, x.get('date', '')), reverse=True)
        recent_transactions = recent_transactions[:10]  # Keep only 10 most recent

        # Build a dedicated list of recent BANK transactions (credits + debits)
        # for the dashboard "Bank Accounts" section.
        bank_transactions.sort(key=lambda x: (x.get('internal_date_ms', 0) or 0, x.get('date', '')), reverse=True)
        recent_bank_transactions = bank_transactions[:10]  # Top 10 most recent bank movements
        
        # 5. Format expense breakdown for pie chart
        expense_breakdown = []
        colors = ['#2E7D32', '#D4A373', '#F4A261', '#1976D2', '#E63946', '#9C27B0', '#00BCD4', '#FF9800']
        total_expenses = sum(expense_categories.values()) or 1
        
        for i, (category, amount) in enumerate(sorted(expense_categories.items(), key=lambda x: x[1], reverse=True)):
            expense_breakdown.append({
                'name': category,
                'value': round((amount / total_expenses) * 100, 1),
                'amount': amount,
                'color': colors[i % len(colors)]
            })
        
        # 6. Get goals/bills progress
        goals = []
        total_goals_target = 0
        total_goals_assigned = 0
        
        try:
            goals_results = goals_collection.get()
            seen_goals = set()
            
            for i in range(len(goals_results['ids'])):
                meta = goals_results['metadatas'][i]
                item_name = meta.get('item', '').lower()
                
                if item_name in seen_goals:
                    continue
                seen_goals.add(item_name)
                
                target = float(meta.get('amount', 0))
                assigned = float(meta.get('assigned', 0))
                total_goals_target += target
                total_goals_assigned += assigned
                
                goals.append({
                    'id': goals_results['ids'][i],
                    'name': meta.get('item', 'Goal'),
                    'target': target,
                    'assigned': assigned,
                    'progress': round((assigned / target * 100) if target > 0 else 0, 1),
                    'deadline': meta.get('deadline', ''),
                    'category': meta.get('category', 'goal')
                })
        except Exception as e:
            print(f"Error getting goals: {e}")
        
        # 7. Calculate business runway (days until cash runs out)
        daily_expense = (cash_out / 30) if cash_out > 0 else 1
        runway_days = int(total_balance / daily_expense) if daily_expense > 0 else 999
        
        # 8. Build summary
        summary = {
            'total_balance': total_balance,
            'cash_in': cash_in,
            'cash_out': cash_out,
            'net_cash_flow': cash_in - cash_out,
            'total_expenses': sum(expense_categories.values()),
            'runway_days': min(runway_days, 365),  # Cap at 1 year
            'goals_progress': round((total_goals_assigned / total_goals_target * 100) if total_goals_target > 0 else 0, 1)
        }
        
        return {
            "status": "success",
            "accounts": accounts,
            "cash_flow": {
                "cash_in": cash_in,
                "cash_out": cash_out,
                "current_balance": total_balance
            },
            "expense_breakdown": expense_breakdown,
            "recent_transactions": recent_transactions[:10],  # Top 10 most recent
            "bank_transactions": recent_bank_transactions,  # Top 10 recent bank credits/debits
            "needs_review": needs_review[:5],  # Top 5 items needing review
            "goals": goals,
            "summary": summary
        }
        
    except Exception as e:
        print(f"Dashboard Error: {str(e)}")
        return {
            "status": "error",
            "error": str(e),
            "accounts": [],
            "cash_flow": {"cash_in": 0, "cash_out": 0, "current_balance": 0},
            "expense_breakdown": [],
            "recent_transactions": [],
            "bank_transactions": [],
            "goals": [],
            "summary": {}
        }

# ========== INVOICES ENDPOINTS ==========

@app.post("/create-invoice")
async def create_invoice(data: dict):
    """
    Create a new invoice for money owed.
    {
        "client_name": "Jimmy Ayo",
        "amount": 50000,
        "description": "Website development",
        "due_date": "2026-06-15",
        "category": "Freelance"
    }
    """
    try:
        import uuid
        from datetime import datetime
        
        user_id = data.get("user_id", "default")
        client_name = data.get("client_name", "Unknown")
        amount = float(data.get("amount", 0))
        description = data.get("description", "")
        due_date = data.get("due_date", "")
        category = data.get("category", "Invoice")
        
        invoice_id = f"inv_{uuid.uuid4().hex[:12]}"
        created_date = datetime.now().strftime("%Y-%m-%d")
        
        invoices_collection.add(
            ids=[invoice_id],
            documents=[f"Invoice for {client_name}: {description} - {amount} NGN"],
            metadatas=[{
                "user_id": user_id,
                "client_name": client_name,
                "amount": amount,
                "description": description,
                "due_date": due_date,
                "category": category,
                "status": "unpaid",
                "created_date": created_date,
                "paid_date": ""
            }]
        )
        
        return {
            "status": "success",
            "invoice_id": invoice_id,
            "message": f"Invoice created for {client_name} - {amount:,.2f} NGN"
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/get-invoices")
async def get_invoices(user_id: str = "default"):
    """Get all invoices with their payment status, scoped to one user."""
    try:
        results = invoices_collection.get(where={"user_id": user_id})
        invoices = []
        
        total_unpaid = 0
        total_paid = 0
        
        for i in range(len(results['ids'])):
            meta = results['metadatas'][i]
            amount = float(meta.get('amount', 0))
            status = meta.get('status', 'unpaid')
            
            if status == 'unpaid':
                total_unpaid += amount
            else:
                total_paid += amount
            
            invoices.append({
                'id': results['ids'][i],
                'client_name': meta.get('client_name', 'Unknown'),
                'amount': amount,
                'description': meta.get('description', ''),
                'due_date': meta.get('due_date', ''),
                'category': meta.get('category', 'Invoice'),
                'status': status,
                'created_date': meta.get('created_date', ''),
                'paid_date': meta.get('paid_date', '')
            })
        
        # Sort by status (unpaid first) then by due date
        invoices.sort(key=lambda x: (x['status'] == 'paid', x['due_date']))
        
        return {
            "status": "success",
            "invoices": invoices,
            "summary": {
                "total_unpaid": total_unpaid,
                "total_paid": total_paid,
                "unpaid_count": len([i for i in invoices if i['status'] == 'unpaid']),
                "paid_count": len([i for i in invoices if i['status'] == 'paid'])
            }
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "invoices": [], "summary": {}}


@app.post("/mark-invoice-paid/{invoice_id}")
async def mark_invoice_paid(invoice_id: str):
    """Mark an invoice as paid."""
    try:
        from datetime import datetime
        
        # Get current invoice data
        result = invoices_collection.get(ids=[invoice_id])
        if not result['ids']:
            return {"status": "error", "error": "Invoice not found"}
        
        meta = result['metadatas'][0]
        meta['status'] = 'paid'
        meta['paid_date'] = datetime.now().strftime("%Y-%m-%d")
        
        # Update the invoice
        invoices_collection.update(
            ids=[invoice_id],
            metadatas=[meta]
        )
        
        return {
            "status": "success",
            "message": f"{meta.get('client_name', 'Client')} has paid {meta.get('amount', 0):,.2f} NGN"
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.delete("/delete-invoice/{invoice_id}")
async def delete_invoice(invoice_id: str):
    """Delete an invoice."""
    try:
        invoices_collection.delete(ids=[invoice_id])
        return {"status": "success", "message": "Invoice deleted"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/categorize-transaction")
async def categorize_transaction(data: dict):
    """
    Categorize a transaction flagged for review from the DASHBOARD inline buttons.

    Bank emails are stored as one JSON blob (not per-record), so we can't reliably
    update an individual email. Instead we record the decision in the shared
    review_queue_collection keyed by source_id - the SAME source of truth the
    Review Queue page uses. This guarantees the choice (a) removes the item from
    "Needs Review" and (b) flows into the expense breakdown on the dashboard.
    {
        "transaction_id": "gmail_xxx",   # the transaction source_id shown on the dashboard
        "category": "Personal Expenses"
    }
    """
    try:
        from datetime import datetime as _dt

        transaction_id = data.get("transaction_id")
        category = normalize_category(data.get("category", "Other"))
        if not transaction_id:
            return {"status": "error", "error": "transaction_id is required"}

        # Find an existing review item for this source_id, or create one.
        existing = review_queue_collection.get(where={"source_id": transaction_id})
        if existing["ids"]:
            rid = existing["ids"][0]
            meta = existing["metadatas"][0] or {}
            meta["status"] = "resolved"
            meta["category"] = category
            meta["user_reply"] = meta.get("user_reply") or category
            meta["resolved_at"] = _dt.now().isoformat()
            review_queue_collection.update(ids=[rid], metadatas=[meta])
        else:
            # No queued nudge yet (the user categorized straight from the
            # dashboard) - persist a minimal resolved record so the dashboard
            # rebuild can see it.
            review_queue_collection.add(
                ids=[f"rev_{transaction_id}"],
                documents=[f"Dashboard categorization: {transaction_id} = {category}"],
                metadatas=[{
                    "user_id": "default",
                    "vendor": "",
                    "amount": float(data.get("amount", 0) or 0),
                    "transaction_type": data.get("type", "debit"),
                    "date": data.get("date", ""),
                    "source_id": transaction_id,
                    "status": "resolved",
                    "nudge": "",
                    "user_reply": category,
                    "category": category,
                    "resolved_at": _dt.now().isoformat(),
                    "created_at": _dt.now().isoformat(),
                }]
            )

        return {
            "status": "success",
            "message": f"Transaction categorized as {category}"
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ============================================
# REVIEW QUEUE SYSTEM (Layer 2: The Chat Nudge & Smart Memory)
# When an unrecognized bank transaction is caught, it lands here so Loamy can
# proactively ask the user what it was for, then learn the vendor for next time.
# ============================================

def _normalize_vendor(name):
    """Deterministic vendor key for memory lookups."""
    return (name or "").strip().lower()


def _extract_vendor_name(narration, tx_type="debit"):
    """
    Deterministically pull a clean counterparty name out of a bank narration.

    Examples:
      "TRF/NIP/Transfer to ABUL-KHAIR ENTERPRISES/..." -> "Abul-Khair Enterprises"
      "POS Purchase CHRISTABEL STORES LAGOS"          -> "Christabel Stores Lagos"
      "FIP:GTB/IBRAHIM MUHAMMAD"                       -> "Ibrahim Muhammad"  (the SENDER on a credit)
      "FIP:MB:OPY/SAMUEL SOPIRIBO IY/SAMUEL..."        -> "Samuel Sopiribo Iy"

    For NIBSS "FIP:" narrations the counterparty is the name AFTER the bank code
    (i.e. after the first "/"), NOT before it. Falls back to a trimmed narration.
    """
    import re
    if not narration:
        return "Unknown Vendor"

    text = str(narration).strip()

    # --- NIBSS FIP format: "FIP:<BANKCODE>/<COUNTERPARTY NAME>[/...]" ---
    # The real other party is the segment right AFTER the first slash.
    fip_match = re.match(r"(?i)\s*fip[:\s]*(?:mb[:\s]*)?[A-Za-z0-9]*/\s*([A-Za-z0-9&'\-\. ]{2,60})", text)
    if fip_match:
        candidate = fip_match.group(1)
        candidate = re.split(r"[/\\|]", candidate)[0]           # first name segment only
        candidate = re.sub(r"\b\d{4,}\b", " ", candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" -.,")
        if candidate:
            return candidate.title()[:50]

    # --- "to X" (debits) / "from X" (credits) ---
    keyword = r"from" if tx_type == "credit" else r"to"
    match = re.search(rf"\b{keyword}\s+([A-Za-z0-9&'\-\. ]{{2,60}})", text, re.IGNORECASE)
    candidate = match.group(1) if match else text

    # Strip common bank prefixes/codes and trailing reference noise
    candidate = re.sub(r"(?i)\b(trf|nip|transfer|pos|purchase|payment|web|mobile|ref|fip)\b", " ", candidate)
    candidate = re.split(r"[/\\|]", candidate)[0]          # cut at separators
    candidate = re.sub(r"\b\d{4,}\b", " ", candidate)       # drop long reference numbers
    candidate = re.sub(r"\s+", " ", candidate).strip(" -.,")

    if not candidate:
        candidate = text[:40]

    # Title-case for readability while preserving all-caps acronyms reasonably
    return candidate.title()[:50]


def _build_nudge_message(user_name, vendor, amount, tx_type="debit"):
    """Compose Loamy's casual clarification question. Pure string logic (deterministic)."""
    first_name = (user_name or "there").split(" ")[0]
    if tx_type == "credit":
        # Money IN - likely a sale or invoice payment.
        return (
            f"Hey {first_name}, you received \u20a6{amount:,.0f} from {vendor}. "
            f"What was this for? (e.g. payment for an invoice, a sale, or a refund)"
        )
    # Money OUT - an expense or transfer.
    return (
        f"Hey {first_name}, I noticed a payment of \u20a6{amount:,.0f} to {vendor}. "
        f"What was it for? (e.g. stock, supplies, a bill, or salaries)"
    )


REVIEW_SCAN_COOLDOWN_SECONDS = 60  # matches the "feels instant" bar for a page open


def _review_scan_cooldown_elapsed(user_id):
    """True if enough time has passed since the last review-queue scan for this
    user. Mirrors _server_sync_cooldown_elapsed's storage pattern (sync_state_collection)
    so we don't add a new dependency just to throttle this."""
    try:
        res = sync_state_collection.get(ids=[f"reviewscan_{user_id}"])
        if res["ids"]:
            last = int((res["metadatas"][0] or {}).get("last_review_scan_at", 0))
            return (int(datetime.now().timestamp()) - last) >= REVIEW_SCAN_COOLDOWN_SECONDS
    except Exception:
        pass
    return True


def _mark_review_scan(user_id):
    sid = f"reviewscan_{user_id}"
    meta = {"user_id": user_id, "last_review_scan_at": int(datetime.now().timestamp())}
    try:
        existing = sync_state_collection.get(ids=[sid])
        if existing["ids"]:
            sync_state_collection.update(ids=[sid], metadatas=[meta])
        else:
            sync_state_collection.add(ids=[sid], documents=[f"review scan stamp {user_id}"], metadatas=[meta])
    except Exception as e:
        print(f"[REVIEW] Could not stamp review scan for {user_id}: {e}")


def _scan_bank_emails_for_review(user_id="default", user_name="there", force=False):
    """
    Scan synced bank-alert emails and auto-queue any uncategorized credit/debit
    whose vendor we don't already recognize. Runs on demand so the Review Queue
    stays fresh without depending on the dashboard being opened. Deterministic logic.

    This does one DB lookup per uncategorized email, so re-running it on every
    single page load/render was making Review Queue slow to open. Throttled to
    once per REVIEW_SCAN_COOLDOWN_SECONDS per user; pass force=True to bypass
    (e.g. right after a fresh Gmail sync).
    """
    if not force and not _review_scan_cooldown_elapsed(user_id):
        return 0
    _mark_review_scan(user_id)
    try:
        # Only this user's synced Gmail blob, never the whole collection.
        gmail_results = gmail_data_collection.get(ids=[f"gmail_{user_id}"])
    except Exception as e:
        print(f"[REVIEW] scan: could not read gmail data: {e}")
        return 0

    # Gmail data is stored as a single JSON blob per user in the document body:
    #   document = json.dumps({"emails": [...], "stats": {...}})
    # so we parse the documents, NOT the record-level metadata.
    emails = []
    for i in range(len(gmail_results.get('ids', []))):
        doc = gmail_results['documents'][i]
        if not doc:
            continue
        try:
            parsed = json.loads(doc)
            emails.extend(parsed.get("emails", []))
        except Exception:
            continue

    added = 0
    for email in emails:
        if not (email.get('is_bank_alert') is True or email.get('is_bank_alert') == 'true'):
            continue

        amount = float(email.get('amount', 0) or 0)
        if amount <= 0:
            continue

        category = email.get('category', '')
        narration = email.get('narration') or email.get('subject') or 'Transaction'
        narration_lower = narration.lower()
        tx_type = email.get('transaction_type', '') or 'debit'
        date = email.get('date', '')
        source_id = email.get('id') or f"{narration_lower[:20]}_{amount}"

        # "Banking" is the placeholder category assigned to every raw bank alert,
        # so treat it as uncategorized for review purposes.
        has_no_category = not category or category.lower() in ['other', 'uncategorized', 'banking', '']
        # NOTE: 'fip' is a NIBSS transfer-reference prefix (e.g. "FIP:GTB/..."),
        # NOT a bank fee, so it must NOT be filtered out here.
        is_not_fee = all(kw not in narration_lower for kw in
                         ['charge', 'fee', 'stamp duty', 'vat', 'levy', 'commission', 'cot', 'maintenance'])
        if not (has_no_category and is_not_fee):
            continue

        vendor_name = _extract_vendor_name(narration, tx_type)
        vendor_key = _normalize_vendor(vendor_name)
        try:
            known = vendor_memory_collection.get(where={"vendor_key": vendor_key})
            already = review_queue_collection.get(where={"source_id": source_id})
            if known["ids"] or already["ids"]:
                continue
            review_queue_collection.add(
                ids=[f"rev_{source_id}"],
                documents=[f"Review: {vendor_name} {amount}"],
                metadatas=[{
                    "user_id": user_id,
                    "vendor": vendor_name,
                    "amount": amount,
                    "transaction_type": tx_type,
                    "date": date,
                    "source_id": source_id,
                    "status": "pending",
                    "nudge": _build_nudge_message(user_name, vendor_name, amount, tx_type),
                    "user_reply": "",
                    "category": "",
                    "created_at": date or ""
                }]
            )
            added += 1
        except Exception as e:
            print(f"[REVIEW] scan add error: {e}")
    print(f"[REVIEW] scan checked {len(emails)} emails, queued {added} new item(s)")
    return added


@app.post("/review-queue/add")
async def add_to_review_queue(data: dict):
    """
    Add an unrecognized bank transaction to the review queue.
    {
        "user_id": "...", "vendor": "ABUL-KHAIR ENTERPRISES",
        "amount": 15000, "transaction_type": "debit",
        "date": "2026-06-08", "source_id": "gmail_xxx" (optional)
    }
    """
    try:
        import uuid as _uuid
        from datetime import datetime as _dt

        user_id = data.get("user_id", "default")
        vendor = (data.get("vendor") or "").strip()
        amount = float(data.get("amount", 0) or 0)
        tx_type = data.get("transaction_type", "debit")

        if not vendor or amount <= 0:
            return {"status": "error", "data": None, "error": "vendor and amount are required"}

        # Smart Memory: if we already know this vendor, auto-resolve (don't nudge)
        vendor_key = _normalize_vendor(vendor)
        known = vendor_memory_collection.get(where={"vendor_key": vendor_key})
        if known["ids"]:
            return {
                "status": "success",
                "data": {"auto_resolved": True, "category": known["metadatas"][0].get("category")},
                "error": None
            }

        review_id = f"rev_{_uuid.uuid4().hex[:12]}"
        user_name = data.get("user_name", "there")
        item = {
            "user_id": user_id,
            "vendor": vendor,
            "amount": amount,
            "transaction_type": tx_type,
            "date": data.get("date", _dt.now().strftime("%Y-%m-%d")),
            "source_id": data.get("source_id", ""),
            "status": "pending",
            "nudge": _build_nudge_message(user_name, vendor, amount, tx_type),
            "user_reply": "",
            "category": "",
            "created_at": _dt.now().isoformat()
        }
        review_queue_collection.add(
            ids=[review_id],
            documents=[f"Review: {vendor} {amount}"],
            metadatas=[item]
        )
        return {"status": "success", "data": {"id": review_id, **item}, "error": None}
    except Exception as e:
        return {"status": "error", "data": None, "error": str(e)}


@app.get("/review-queue/{user_id}")
async def get_review_queue(user_id: str):
    """Return all pending review items for a user (newest first).
    Also scans synced bank emails first so the queue stays fresh on its own."""
    try:
        _scan_bank_emails_for_review(user_id)
        results = review_queue_collection.get(where={"user_id": user_id})
        items = []
        resolved = []
        for i in range(len(results["ids"])):
            meta = results["metadatas"][i]
            status = meta.get("status")
            if status == "pending":
                items.append({"id": results["ids"][i], **meta})
            elif status == "resolved":
                resolved.append({"id": results["ids"][i], **meta})
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        # Newest logged first; cap the history so the page stays light.
        resolved.sort(key=lambda x: x.get("resolved_at", ""), reverse=True)
        resolved = resolved[:20]
        return {
            "status": "success",
            "data": {"items": items, "count": len(items),
                     "resolved": resolved, "resolved_count": len(resolved)},
            "error": None,
        }
    except Exception as e:
        return {"status": "error", "data": None, "error": str(e)}


@app.post("/review-queue/resolve")
async def resolve_review_item(data: dict):
    """
    User replies to a nudge. Gemini extracts the category from the free-text reply,
    we tag the underlying transaction, save a vendor memory rule, and close the item.
    {
        "review_id": "rev_xxx",
        "reply": "Sugar for stock"
    }
    """
    try:
        from datetime import datetime as _dt

        review_id = data.get("review_id")
        reply = (data.get("reply") or "").strip()
        if not review_id or not reply:
            return {"status": "error", "data": None, "error": "review_id and reply are required"}

        result = review_queue_collection.get(ids=[review_id])
        if not result["ids"]:
            return {"status": "error", "data": None, "error": "Review item not found"}

        meta = result["metadatas"][0]
        vendor = meta.get("vendor", "")

        # Use Gemini ONLY to detect the category from the free-text reply (intent extraction).
        category = "Other"
        try:
            classify_prompt = f"""You are categorizing an expense based on a short user note.
Vendor: {vendor}
User's note: "{reply}"

Pick the SINGLE best category from this list ONLY:
Stock, Groceries, Ingredients, Transport, Bills, Salaries, Equipment, Rent, Marketing, Food, Shopping, Hardware, Electronics, Personal Expenses, Other

If the note describes a personal (non-business) purchase, choose "Personal Expenses".

Reply with ONLY the category name, nothing else."""
            ai_resp = model.generate_content(classify_prompt)
            candidate = (ai_resp.text or "").strip().split("\n")[0].strip()
            # Longer names first so "Personal Expenses" matches before "Personal".
            valid = ["Personal Expenses", "Electronics", "Hardware", "Stock", "Groceries",
                     "Ingredients", "Transport", "Bills", "Salaries", "Equipment", "Rent",
                     "Marketing", "Food", "Shopping", "Other"]
            candidate_lower = candidate.lower()
            for v in valid:
                if v.lower() in candidate_lower:
                    category = v
                    break
            # Safety net: if the user literally wrote "personal", honor it even if
            # the model picked something else.
            if "personal" in reply.lower():
                category = "Personal Expenses"
        except Exception as e:
            print(f"[REVIEW] Category classify error: {e}")

        # 1. Close the review item
        meta["status"] = "resolved"
        meta["user_reply"] = reply
        meta["category"] = category
        meta["resolved_at"] = _dt.now().isoformat()
        review_queue_collection.update(ids=[review_id], metadatas=[meta])

        # 2. Tag the underlying bank transaction (if linked)
        source_id = meta.get("source_id", "")
        if source_id:
            try:
                tx = gmail_data_collection.get(ids=[source_id])
                if tx["ids"]:
                    tx_meta = tx["metadatas"][0]
                    tx_meta["category"] = category
                    tx_meta["reviewed"] = True
                    gmail_data_collection.update(ids=[source_id], metadatas=[tx_meta])
            except Exception as e:
                print(f"[REVIEW] Could not tag transaction: {e}")

        # 3. Smart Memory: remember this vendor -> category for next time
        try:
            import uuid as _uuid
            vendor_key = _normalize_vendor(vendor)
            existing = vendor_memory_collection.get(where={"vendor_key": vendor_key})
            if existing["ids"]:
                vendor_memory_collection.update(
                    ids=[existing["ids"][0]],
                    metadatas=[{"vendor_key": vendor_key, "vendor": vendor,
                                "category": category, "user_id": meta.get("user_id", "default")}]
                )
            else:
                vendor_memory_collection.add(
                    ids=[f"vm_{_uuid.uuid4().hex[:12]}"],
                    documents=[f"{vendor} = {category}"],
                    metadatas=[{"vendor_key": vendor_key, "vendor": vendor,
                                "category": category, "user_id": meta.get("user_id", "default")}]
                )
        except Exception as e:
            print(f"[REVIEW] Vendor memory save error: {e}")

        confirmation = f"Got it \u2014 logged \u20a6{float(meta.get('amount', 0)):,.0f} to {vendor} under {category}. I'll remember {vendor} next time."
        return {
            "status": "success",
            "data": {"category": category, "vendor": vendor, "confirmation": confirmation},
            "error": None
        }
    except Exception as e:
        return {"status": "error", "data": None, "error": str(e)}


@app.post("/review-queue/dismiss")
async def dismiss_review_item(data: dict):
    """Dismiss/ignore a review item without categorizing."""
    try:
        review_id = data.get("review_id")
        result = review_queue_collection.get(ids=[review_id])
        if not result["ids"]:
            return {"status": "error", "data": None, "error": "Review item not found"}
        meta = result["metadatas"][0]
        meta["status"] = "dismissed"
        review_queue_collection.update(ids=[review_id], metadatas=[meta])
        return {"status": "success", "data": {"id": review_id}, "error": None}
    except Exception as e:
        return {"status": "error", "data": None, "error": str(e)}


@app.post("/review-queue/seed-demo")
async def seed_demo_review(data: dict):
    """Create a sample unrecognized-transaction nudge so the user can try the flow.

    This writes DIRECTLY to the queue and intentionally bypasses the vendor-memory
    auto-resolve check, so the demo nudge ALWAYS appears even if the sample vendor
    was categorized in a previous test run.
    """
    try:
        import uuid as _uuid
        from datetime import datetime as _dt

        user_id = data.get("user_id", "default")
        user_name = data.get("user_name", "there")
        vendor = "Abul-Khair Enterprises"
        amount = 15000.0
        tx_type = "debit"

        review_id = f"rev_demo_{_uuid.uuid4().hex[:8]}"
        item = {
            "user_id": user_id,
            "vendor": vendor,
            "amount": amount,
            "transaction_type": tx_type,
            "date": _dt.now().strftime("%Y-%m-%d"),
            "source_id": f"demo_{review_id}",
            "status": "pending",
            "nudge": _build_nudge_message(user_name, vendor, amount, tx_type),
            "user_reply": "",
            "category": "",
            "is_demo": "true",
            "created_at": _dt.now().isoformat()
        }
        review_queue_collection.add(
            ids=[review_id],
            documents=[f"Review demo: {vendor} {amount}"],
            metadatas=[item]
        )
        print(f"[REVIEW] seeded demo nudge {review_id} for user {user_id}")
        return {"status": "success", "data": {"id": review_id, **item}, "error": None}
    except Exception as e:
        print(f"[REVIEW] seed-demo error: {e}")
        return {"status": "error", "data": None, "error": str(e)}


@app.post("/dedupe-goals")
async def dedupe_goals():
    """Remove duplicate goals/bills from database, keeping only the one with highest assigned amount"""
    try:
        results = goals_collection.get()
        seen = {}  # item_name -> {id, assigned, index}
        to_delete = []
        
        for i in range(len(results['ids'])):
            meta = results['metadatas'][i]
            item_name = meta.get('item', '').lower()
            assigned = float(meta.get('assigned', 0))
            goal_id = results['ids'][i]
            
            if item_name in seen:
                # Duplicate found - keep the one with higher assigned amount
                if assigned > seen[item_name]['assigned']:
                    # New one is better, delete the old one
                    to_delete.append(seen[item_name]['id'])
                    seen[item_name] = {'id': goal_id, 'assigned': assigned}
                else:
                    # Old one is better, delete this one
                    to_delete.append(goal_id)
            else:
                seen[item_name] = {'id': goal_id, 'assigned': assigned}
        
        # Delete duplicates
        if to_delete:
            goals_collection.delete(ids=to_delete)
        
        return {"status": "success", "deleted": len(to_delete)}
    except Exception as e:
        print(f"Dedupe Error: {str(e)}")
        return {"status": "error", "error": str(e)}

@app.post("/add-bill")
async def add_bill(data: dict):
    """Add a new bill category"""
    try:
        item_name = to_sentence_case(data.get("item", ""))
        amount = float(data.get("amount", 0))
        deadline = data.get("deadline", "Monthly")  # Can be "Monthly", "Weekly", "Yearly", or specific date "YYYY-MM-DD"
        
        goal_id = f"goal_{os.urandom(4).hex()}"
        
        goals_collection.add(
            documents=[f"Bill: {item_name} with target ${amount}, due {deadline}"],
            metadatas=[{
                "item": item_name,
                "amount": amount,
                "deadline": deadline,
                "category": "bill"
            }],
            ids=[goal_id]
        )
        print(f"Bill Added: {item_name} (due: {deadline})")
        return {"status": "success", "id": goal_id}
    except Exception as e:
        print(f"Add Bill Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/update-goal")
async def update_goal(data: dict):
    """Update a goal's target amount (Supabase-backed, scoped to the user)."""
    try:
        user_id = data.get("user_id", "default")
        item_name = data.get("item", "")
        new_amount = float(data.get("amount", 0))
        deadline = data.get("deadline", "Monthly")
        category = data.get("category", "goal")
        assigned = float(data.get("assigned", 0))

        # Find existing goal by name within THIS user's goals.
        res = database.get_goals(user_id)
        rows = res["data"] if res["status"] == "success" else []
        goal_id = None
        for row in rows:
            if (row.get("item") or "").lower() == item_name.lower():
                goal_id = row.get("id")
                # Keep the existing category if not specified.
                if category == "goal":
                    category = row.get("category", "goal")
                break

        if not goal_id:
            goal_id = f"goal_{os.urandom(4).hex()}"

        # Upsert keyed on the goal id (add_goal upserts on conflict).
        saved = database.add_goal(
            user_id=user_id, goal_id=goal_id,
            item=to_sentence_case(item_name),
            amount=new_amount, deadline=deadline,
            category=category, assigned=assigned,
            document=f"Goal: Save {new_amount} for {item_name} by {deadline}",
        )
        if saved["status"] != "success":
            raise HTTPException(status_code=500, detail=saved["error"])
        print(f"Goal Updated: {item_name} -> {new_amount}")
        return {"status": "success", "id": goal_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Update Goal Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete-goal")
async def delete_goal(data: dict):
    """Delete a goal/bill by name (Supabase-backed, scoped to the user)."""
    try:
        user_id = data.get("user_id", "default")
        item_name = data.get("item", "")

        # Find goal by name within THIS user's goals.
        res = database.get_goals(user_id)
        rows = res["data"] if res["status"] == "success" else []
        goal_id = None
        for row in rows:
            if (row.get("item") or "").lower() == item_name.lower():
                goal_id = row.get("id")
                break

        if not goal_id:
            raise HTTPException(status_code=404, detail="Goal not found")

        removed = database.delete_goal(goal_id)
        if removed["status"] != "success":
            raise HTTPException(status_code=500, detail=removed["error"])
        print(f"Goal Deleted: {item_name}")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Delete Goal Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/rename-goal")
async def rename_goal(data: dict):
    """Rename a goal/bill"""
    try:
        old_name = data.get("old_name", "")
        new_name = to_sentence_case(data.get("new_name", ""))
        
        # Find goal by name
        results = goals_collection.get()
        goal_id = None
        goal_meta = None
        
        for i in range(len(results['ids'])):
            if results['metadatas'][i]['item'].lower() == old_name.lower():
                goal_id = results['ids'][i]
                goal_meta = results['metadatas'][i]
                break
        
        if not goal_id:
            raise HTTPException(status_code=404, detail="Goal not found")
        
        # Delete old entry
        goals_collection.delete(ids=[goal_id])
        
        # Add with new name but keep all other data
        goals_collection.add(
            documents=[f"Goal: Save ${goal_meta['amount']} for {new_name}"],
            metadatas=[{
                "item": new_name,
                "amount": goal_meta['amount'],
                "deadline": goal_meta.get('deadline', 'Monthly'),
                "category": goal_meta.get('category', 'goal'),
                "assigned": goal_meta.get('assigned', 0)
            }],
            ids=[goal_id]
        )
        print(f"Goal Renamed: {old_name} -> {new_name}")
        return {"status": "success", "new_name": new_name}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Rename Goal Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/assign-to-goal")
async def assign_to_goal(data: dict):
    """Assign money from accounts to a goal"""
    try:
        item_name = data.get("item", "")
        assign_amount = float(data.get("amount", 0))
        
        # Find existing goal by name
        results = goals_collection.get()
        goal_id = None
        goal_meta = None
        
        for i in range(len(results['ids'])):
            if results['metadatas'][i]['item'].lower() == item_name.lower():
                goal_id = results['ids'][i]
                goal_meta = results['metadatas'][i]
                break
        
        if not goal_id:
            raise HTTPException(status_code=404, detail="Goal not found")
        
        # Calculate new assigned amount
        current_assigned = float(goal_meta.get('assigned', 0))
        new_assigned = current_assigned + assign_amount
        
        # Delete old entry
        goals_collection.delete(ids=[goal_id])
        
        # Add updated goal with new assigned amount
        goals_collection.add(
            documents=[f"Goal: Save ${goal_meta['amount']} for {item_name}, assigned ${new_assigned}"],
            metadatas=[{
                "item": goal_meta['item'],
                "amount": goal_meta['amount'],
                "deadline": goal_meta.get('deadline', 'Monthly'),
                "category": goal_meta.get('category', 'goal'),
                "assigned": new_assigned
            }],
            ids=[goal_id]
        )
        print(f"Assigned ${assign_amount} to {item_name}. Total assigned: ${new_assigned}")
        return {"status": "success", "assigned": new_assigned}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Assign Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ========== ACCOUNT ENDPOINTS ==========

@app.get("/get-accounts")
async def get_accounts(user_id: str = "default"):
    """Get all bank accounts for a user (Supabase-backed).

    The UI uses `nickname`/`type`, which we keep in metadata while the amount
    lives in the typed `account_name`/`balance` columns, so the response shape
    the frontend expects is preserved exactly."""
    try:
        res = database.get_accounts(user_id)
        rows = res["data"] if res["status"] == "success" else []
        accounts = []
        for row in rows:
            meta = row.get("metadata") or {}
            accounts.append({
                "id": row.get("id"),
                "nickname": meta.get("nickname") or row.get("account_name") or "",
                "balance": row.get("balance", 0),
                "type": meta.get("type", "Checking"),
            })
        return {"accounts": accounts}
    except Exception as e:
        return {"accounts": [], "error": str(e)}

@app.post("/add-account")
async def add_account(data: dict):
    """Add a new bank account (Supabase-backed)."""
    try:
        user_id = data.get("user_id", "default")
        nickname = data.get("nickname")
        balance = float(data.get("balance", 0))
        acc_type = data.get("type", "Checking")

        account_id = f"acc_{os.urandom(4).hex()}"

        saved = database.add_account(
            user_id=user_id, account_id=account_id,
            account_name=nickname, balance=balance,
            # legacy UI fields kept in metadata so nothing is lost
            nickname=nickname, type=acc_type,
            document=f"Account: {nickname} with balance {balance}",
        )
        if saved["status"] != "success":
            raise HTTPException(status_code=500, detail=saved["error"])
        print(f"Account Added: {nickname}")
        return {"status": "success", "id": account_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Add Account Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/update-account/{account_id}")
async def update_account(account_id: str, data: dict):
    """Update an existing bank account (Supabase-backed upsert)."""
    try:
        user_id = data.get("user_id", "default")
        nickname = data.get("nickname")
        balance = float(data.get("balance", 0))
        acc_type = data.get("type", "Checking")

        saved = database.add_account(
            user_id=user_id, account_id=account_id,
            account_name=nickname, balance=balance,
            nickname=nickname, type=acc_type,
            document=f"Account: {nickname} with balance {balance}",
        )
        if saved["status"] != "success":
            raise HTTPException(status_code=500, detail=saved["error"])
        print(f"Account Updated: {nickname}")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Update Account Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete-account/{account_id}")
async def delete_account(account_id: str):
    """Delete a bank account (Supabase-backed)."""
    try:
        removed = database.delete_account(account_id)
        if removed["status"] != "success":
            raise HTTPException(status_code=500, detail=removed["error"])
        print(f"Account Deleted: {account_id}")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Delete Account Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ========== BULK ALLOCATION ENDPOINT ==========

@app.post("/allocate-funds")
async def allocate_funds(data: dict):
    """
    Allocate funds to multiple goals/bills at once.
    Expected format: {"allocations": [{"item": "Rent", "amount": 500}, {"item": "Savings", "amount": 200}]}
    """
    try:
        user_id = data.get("user_id", "default")
        allocations = data.get("allocations", [])
        print(f"[ALLOCATE] Received allocations: {allocations}")
        updated = []

        # Get this user's goals once (Supabase-backed).
        res = database.get_goals(user_id)
        rows = res["data"] if res["status"] == "success" else []
        print(f"[ALLOCATE] Found {len(rows)} goals in database")

        # Index by lowercased item for O(1) matching.
        by_item = {(r.get("item") or "").lower().strip(): r for r in rows}

        for alloc in allocations:
            item_name = alloc.get("item", "").lower().strip()
            add_amount = float(alloc.get("amount", 0))

            print(f"[ALLOCATE] Processing: {item_name} = {add_amount}")

            if not item_name or add_amount <= 0:
                print(f"[ALLOCATE] Skipping invalid: {item_name}")
                continue

            row = by_item.get(item_name)
            if not row:
                print(f"[ALLOCATE] No match found for: {item_name}")
                continue

            # Deterministic Python does the math (Ledger rule); DB just stores it.
            new_assigned = float(row.get("assigned", 0) or 0) + add_amount
            saved = database.update_goal(row.get("id"), assigned=new_assigned)
            if saved["status"] == "success":
                updated.append({"item": row.get("item"), "new_assigned": new_assigned})
                print(f"[ALLOCATE] Updated {row.get('item')} to {new_assigned}")

        print(f"[ALLOCATE] Total updated: {len(updated)}")
        return {"status": "success", "updated": updated}
    except Exception as e:
        print(f"Allocation Error: {str(e)}")
        return {"status": "error", "error": str(e)}

# ========== EXPENSE ENDPOINTS ==========

@app.get("/get-expenses")
async def get_expenses(user_id: str = "default"):
    """Get all expenses for a user (Supabase-backed): manual expense entries
    PLUS transactions (uploaded receipts / synced bank debits), merged into the
    single shape the frontend expects."""
    try:
        expenses = []

        # 1. Manually added expenses.
        exp_res = database.get_expenses(user_id)
        for row in (exp_res["data"] if exp_res["status"] == "success" else []):
            meta = row.get("metadata") or {}
            expenses.append({
                "id": row.get("id"),
                "description": row.get("description", ""),
                "amount": row.get("amount", 0),
                "category": row.get("category", "other"),
                "date": row.get("date", ""),
                "notes": meta.get("notes", ""),
            })

        # 2. Transactions (uploaded receipts / synced debits).
        tx_res = database.get_transactions(user_id)
        for row in (tx_res["data"] if tx_res["status"] == "success" else []):
            expenses.append({
                "id": row.get("id"),
                "description": row.get("vendor") or "Unknown",
                "amount": row.get("amount", 0),
                "category": row.get("category", "other"),
                "date": row.get("date", ""),
                "notes": "From receipt/bank sync",
            })

        return {"expenses": expenses}
    except Exception as e:
        return {"expenses": [], "error": str(e)}

@app.post("/add-expense")
async def add_expense(data: dict):
    """Add a new expense (Supabase-backed)."""
    try:
        user_id = data.get("user_id", "default")
        description = data.get("description", "")
        amount = float(data.get("amount", 0))
        category = data.get("category", "other")
        date = data.get("date", datetime.now().isoformat())
        notes = data.get("notes", "")

        expense_id = f"exp_{os.urandom(4).hex()}"

        saved = database.add_expense(
            user_id=user_id, expense_id=expense_id,
            description=description, amount=amount,
            category=category, date=date, notes=notes,
            document=f"Expense: {description} - {amount} on {date}",
        )
        if saved["status"] != "success":
            raise HTTPException(status_code=500, detail=saved["error"])
        print(f"Expense Added: {description} - {amount}")
        return {"status": "success", "id": expense_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Add Expense Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/update-expense")
async def update_expense(data: dict):
    """Update an existing expense (Supabase-backed upsert)."""
    try:
        user_id = data.get("user_id", "default")
        expense_id = data.get("id")
        description = data.get("description", "")
        amount = float(data.get("amount", 0))
        category = data.get("category", "other")
        date = data.get("date", datetime.now().isoformat())
        notes = data.get("notes", "")

        saved = database.add_expense(
            user_id=user_id, expense_id=expense_id,
            description=description, amount=amount,
            category=category, date=date, notes=notes,
            document=f"Expense: {description} - {amount} on {date}",
        )
        if saved["status"] != "success":
            raise HTTPException(status_code=500, detail=saved["error"])
        print(f"Expense Updated: {description} - {amount}")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Update Expense Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/delete-expense")
async def delete_expense(data: dict):
    """Delete an expense: try the manual expenses table first, then transactions
    (uploaded receipts / synced debits share the same id space in the UI)."""
    try:
        expense_id = data.get("id")

        # Manual expenses first.
        try:
            database.delete_expense(expense_id)
            print(f"Expense Deleted from expenses: {expense_id}")
        except Exception:
            pass

        # Then transactions.
        try:
            database.delete_transaction(expense_id)
            print(f"Expense Deleted from transactions: {expense_id}")
        except Exception:
            pass

        return {"status": "success"}
    except Exception as e:
        print(f"Delete Expense Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ==========================================
# AUTHENTICATION ENDPOINTS
# ==========================================

def hash_password(password: str) -> str:
    """Hash password using SHA-256 with salt"""
    salt = "fintech_ai_salt_2024"  # In production, use unique salt per user
    return hashlib.sha256((password + salt).encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash"""
    return hash_password(password) == hashed


# ============================================
# CHAT MANAGEMENT ENDPOINTS
# ============================================

@app.get("/get-chats")
async def get_chats(user_id: str = "default"):
    """Get all chats for listing in sidebar, scoped to one user (Supabase)."""
    try:
        res = database.get_chats(user_id)
        if res["status"] != "success":
            return {"chats": [], "error": res["error"]}

        chats = []
        for row in res["data"]:
            messages = row.get("messages") or []
            chats.append({
                "id": row["id"],
                "title": row.get("title", "New Chat"),
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", ""),
                "message_count": len(messages),
            })
        # Already ordered by updated_at desc in the query.
        return {"chats": chats}

    except Exception as e:
        print(f"Get chats error: {str(e)}")
        return {"chats": [], "error": str(e)}


@app.post("/create-chat")
async def create_chat(data: dict):
    """Create a new chat"""
    try:
        user_id = data.get("user_id", "default")
        first_message = data.get("first_message", "New Chat")
        messages = data.get("messages", [])
        
        chat_id = f"chat_{secrets.token_hex(8)}"

        res = database.add_chat(
            user_id=user_id, chat_id=chat_id,
            title=first_message[:50], messages=messages,
        )
        if res["status"] != "success":
            return {"status": "error", "error": res["error"]}

        return {"status": "success", "id": chat_id, "title": first_message[:50]}
        
    except Exception as e:
        print(f"Create chat error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.get("/get-chat/{chat_id}")
async def get_chat(chat_id: str):
    """Get a specific chat thread by ID (Supabase)."""
    try:
        res = database.get_chat(chat_id)
        if res["status"] != "success" or not res["data"]:
            return {"status": "error", "error": "Chat not found"}

        row = res["data"]
        return {
            "status": "success",
            "id": chat_id,
            "title": row.get("title", "New Chat"),
            "messages": row.get("messages") or [],
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
        }

    except Exception as e:
        print(f"Get chat error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.put("/update-chat/{chat_id}")
async def update_chat(chat_id: str, data: dict):
    """Update chat messages (Supabase)."""
    try:
        messages = data.get("messages", [])
        res = database.update_chat(chat_id, messages=messages)
        if res["status"] != "success":
            return {"status": "error", "error": res["error"]}
        return {"status": "success"}

    except Exception as e:
        print(f"Update chat error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.delete("/delete-chat/{chat_id}")
async def delete_chat(chat_id: str):
    """Delete a chat thread (Supabase)."""
    try:
        res = database.delete_chat(chat_id)
        if res["status"] != "success":
            return {"status": "error", "error": res["error"]}
        return {"status": "success"}
    except Exception as e:
        print(f"Delete chat error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.put("/rename-chat/{chat_id}")
async def rename_chat(chat_id: str, data: dict):
    """Rename a chat thread (Supabase)."""
    try:
        new_title = data.get("title", "Chat")
        res = database.update_chat(chat_id, title=new_title[:50])
        if res["status"] != "success":
            return {"status": "error", "error": res["error"]}
        return {"status": "success"}

    except Exception as e:
        print(f"Rename chat error: {str(e)}")
        return {"status": "error", "error": str(e)}


# ============================================
# ONBOARDING (Regional banks + profile submit)
# ============================================
# Single responsibility per endpoint (Swap-and-Plug). Standard API contract:
# every response is {status, data, error}.

# Simple in-memory cache so a country's bank list is generated by the AI only
# once, then served instantly (no repeat AI latency or token cost).
_ONBOARDING_BANK_CACHE = {}


@app.get("/api/onboarding/banks")
async def get_onboarding_banks(country: str = ""):
    """
    Return the top commercial banks and fintechs for a country as
    [{"name": ..., "sender_domains": [...]}]. Uses Gemini once per country,
    then serves from cache.
    """
    try:
        key = (country or "").strip().lower()
        if not key:
            return {"status": "error", "data": None, "error": "country is required"}

        # Cache hit -> return immediately.
        if key in _ONBOARDING_BANK_CACHE:
            return {"status": "success", "data": {"banks": _ONBOARDING_BANK_CACHE[key], "cached": True}, "error": None}

        prompt = (
            f"List the top 20 commercial banks and digital fintech platforms in {country}. "
            "Return ONLY a raw JSON array (no markdown, no code fences). Each item must be an "
            'object with exactly two keys: "name" (the common full bank name) and '
            '"sender_domains" (an array of the domains their transaction alert emails come from). '
            'Example: [{"name": "First Bank of Nigeria", "sender_domains": ["firstbanknigeria.com"]}].'
        )

        banks = []
        try:
            resp = model.generate_content(prompt)
            raw = (resp.text or "").strip()
            # Strip accidental code fences.
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
            # Grab the JSON array slice defensively (AI may add stray text).
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1:
                parsed = json.loads(raw[start:end + 1])
                for item in parsed:
                    if isinstance(item, dict) and item.get("name"):
                        banks.append({
                            "name": str(item["name"]).strip(),
                            "sender_domains": item.get("sender_domains", []) or [],
                        })
        except Exception as ai_err:
            print(f"[Onboarding] Bank AI/parse error for {country}: {ai_err}")

        # Defensive fallback so the UI never breaks on an empty AI response.
        if not banks:
            banks = [
                {"name": "Other / Not listed", "sender_domains": []},
            ]

        _ONBOARDING_BANK_CACHE[key] = banks
        return {"status": "success", "data": {"banks": banks, "cached": False}, "error": None}
    except Exception as e:
        print(f"[Onboarding] get_onboarding_banks error: {e}")
        return {"status": "error", "data": None, "error": str(e)}


@app.post("/api/onboarding/submit")
async def submit_onboarding(data: dict):
    """
    Commit the user's onboarding profile (business_type, selected_bank,
    sender_domains) onto their user record and flip their status to ACTIVE_CFO.
    """
    try:
        user_id = data.get("user_id")
        if not user_id:
            return {"status": "error", "data": None, "error": "user_id is required"}

        business_type = data.get("business_type", "")
        selected_bank = data.get("selected_bank", "")
        sender_domains = data.get("sender_domains", []) or []
        country = data.get("country", "")

        existing = database.get_user_by_id(user_id)
        if existing["status"] != "success" or not existing["data"]:
            return {"status": "error", "data": None, "error": "User not found"}

        updated = database.update_user(
            user_id,
            business_type=business_type,
            country=country,
            selected_bank=selected_bank,
            # Supabase sender_domains is jsonb, so keep the real array (no CSV hack).
            sender_domains=sender_domains,
            status="ACTIVE_CFO",
        )
        if updated["status"] != "success":
            return {"status": "error", "data": None, "error": updated["error"]}

        print(f"[Onboarding] Profile committed for {user_id}: {business_type} / {selected_bank}")
        return {
            "status": "success",
            "data": {"user_id": user_id, "status": "ACTIVE_CFO"},
            "error": None,
        }
    except Exception as e:
        print(f"[Onboarding] submit_onboarding error: {e}")
        return {"status": "error", "data": None, "error": str(e)}


@app.post("/link-whatsapp")
async def link_whatsapp(data: dict):
    """Link a WhatsApp number to a Loamy account so the WhatsApp advisor can
    resolve incoming messages to the right user (and see their real balance).

    Stores the number digits-only on the user's row. ensure_user first so this
    works even for accounts not yet mirrored into Supabase.
    """
    try:
        user_id = data.get("user_id")
        phone = data.get("phone_number") or data.get("phone") or ""
        if not user_id:
            return {"status": "error", "error": "user_id is required"}

        digits = database.normalize_phone(phone)
        if len(digits) < 7:
            return {"status": "error", "error": "Please enter a valid WhatsApp number with country code."}

        # Make sure the user row exists, then save the normalized number.
        database.ensure_user(user_id)

        # Prevent linking the same number to two accounts.
        clash = database.get_user_by_phone(digits)
        if (clash["status"] == "success" and clash["data"]
                and clash["data"].get("user_id") != user_id):
            return {"status": "error", "error": "This WhatsApp number is already linked to another account."}

        updated = database.update_user(user_id, phone_number=digits)
        if updated["status"] != "success":
            return {"status": "error", "error": updated["error"]}

        print(f"[WhatsApp] Linked number {digits} -> {user_id}")
        return {"status": "success", "data": {"user_id": user_id, "phone_number": digits}, "error": None}
    except Exception as e:
        print(f"[WhatsApp] link_whatsapp error: {e}")
        return {"status": "error", "error": str(e)}


@app.get("/whatsapp-link-status")
async def whatsapp_link_status(user_id: str):
    """Return whether this account has a WhatsApp number linked (for the UI)."""
    try:
        res = database.get_user_by_id(user_id)
        row = res.get("data") if res.get("status") == "success" else None
        phone = (row or {}).get("phone_number") or ""
        return {"status": "success", "linked": bool(phone), "phone_number": phone}
    except Exception as e:
        return {"status": "error", "linked": False, "error": str(e)}


@app.post("/signup")
async def signup(data: dict):
    """Create a new user account"""
    try:
        name = data.get("name", "").strip()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        
        # Validation
        if not name or not email or not password:
            raise HTTPException(status_code=400, detail="All fields are required")
        
        if len(password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        
        if "@" not in email or "." not in email:
            raise HTTPException(status_code=400, detail="Invalid email address")
        
        # Check if user already exists (Supabase-backed)
        existing = database.get_user_by_email(email)
        existing_row = existing["data"] if existing["status"] == "success" else None
        if existing_row:
            if existing_row.get("password_hash"):
                # A real password is already set on this email - genuine duplicate.
                raise HTTPException(status_code=400, detail="An account with this email already exists")

            # This email was only ever registered via Google Sign-In (blank
            # password_hash), so email/password login could never work for it.
            # Treat this "signup" as setting a password on that same account
            # instead of blocking the user with a duplicate-email error.
            password_hash = hash_password(password)
            updated = database.update_user(
                existing_row["user_id"],
                password_hash=password_hash,
                full_name=name or existing_row.get("full_name"),
            )
            if updated["status"] != "success":
                raise HTTPException(status_code=500, detail=updated["error"])

            print(f"Password set for existing Google account: {email} (ID: {existing_row['user_id']})")
            return {
                "status": "success",
                "user_id": existing_row["user_id"],
                "email": email,
                "name": name or existing_row.get("full_name"),
            }

        # Create user in Supabase
        user_id = f"user_{secrets.token_hex(8)}"
        password_hash = hash_password(password)

        created = database.create_user(
            user_id=user_id,
            email=email,
            password_hash=password_hash,
            full_name=name,
            created_at_original=datetime.now().isoformat(),
        )
        if created["status"] != "success":
            raise HTTPException(status_code=500, detail=created["error"])

        print(f"New user created: {email} (ID: {user_id})")
        
        return {
            "status": "success",
            "user_id": user_id,
            "email": email,
            "name": name
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Signup Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/login")
async def login(data: dict):
    """Authenticate user and return session"""
    try:
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        
        if not email or not password:
            raise HTTPException(status_code=400, detail="Email and password are required")
        
        # Find user by email (Supabase-backed)
        lookup = database.get_user_by_email(email)
        user_found = lookup["data"] if lookup["status"] == "success" else None

        if not user_found:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        # This account was only ever registered via Google Sign-In, so it has no
        # password to check against - a plain "invalid password" would be
        # misleading since no password could ever match. Point them to the fix.
        if not user_found.get('password_hash'):
            raise HTTPException(
                status_code=401,
                detail="This account was created with Google Sign-In and has no password yet. "
                       "Use \"Continue with Google\", or go to Sign Up with this same email to set one.",
            )

        # Verify password
        if not verify_password(password, user_found.get('password_hash', '')):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        print(f"User logged in: {email}")

        return {
            "status": "success",
            "user_id": user_found.get('user_id'),
            "email": user_found.get('email'),
            "name": user_found.get('full_name'),
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Login Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/check-session/{user_id}")
async def check_session(user_id: str):
    """Verify if a user session is valid"""
    try:
        lookup = database.get_user_by_id(user_id)
        meta = lookup["data"] if lookup["status"] == "success" else None
        if meta:
            return {
                "status": "valid",
                "user_id": user_id,
                "email": meta.get('email'),
                "name": meta.get('full_name'),
            }
        return {"status": "invalid"}

    except Exception as e:
        print(f"Check Session Error: {str(e)}")
        return {"status": "error", "error": str(e)}


# ==========================================
# GOOGLE OAUTH & GMAIL API ENDPOINTS
# ==========================================

# Google OAuth credentials — loaded from the environment, never hardcoded.
# Put the real values in your local .env (which is gitignored) so they never
# reach version control (Infrastructure Independence rule).
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    print("[Google OAuth] WARNING: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. "
          "Add them to your .env for Google Sign-In and Gmail sync to work.")

import requests


@app.post("/google-auth")
async def google_auth(data: dict):
    """Handle Google Sign-In for login/signup"""
    try:
        code = data.get("code")
        redirect_uri = data.get("redirect_uri", "http://127.0.0.1:5500/login.html")
        
        # Exchange code for tokens with Google
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        }
        
        response = requests.post(token_url, data=token_data)
        token_response = response.json()
        
        if "access_token" not in token_response:
            print(f"Token exchange failed: {token_response}")
            return {"status": "error", "error": token_response.get("error_description", "Failed to get access token")}
        
        access_token = token_response["access_token"]
        
        # Get user info from Google
        user_info_url = "https://www.googleapis.com/oauth2/v2/userinfo"
        headers = {"Authorization": f"Bearer {access_token}"}
        user_response = requests.get(user_info_url, headers=headers)
        user_data = user_response.json()
        
        email = user_data.get("email", "").lower()
        name = user_data.get("name", "")
        
        if not email:
            return {"status": "error", "error": "Could not get email from Google"}

        # Supabase `users` is the ONE identity table every other table (chats,
        # gmail_data, accounts) has a foreign key to - so Google Sign-In must
        # look up/create the account there directly, exactly like /signup and
        # /login already do. (Previously this checked an in-memory ChromaDB
        # collection instead, which could hand back a user_id Supabase had
        # never heard of, causing every "insert" for that account to fail with
        # a foreign-key error - the "chats not showing" / "can't chat" bug.)
        lookup = database.get_user_by_email(email)
        user_found = lookup["data"] if lookup["status"] == "success" else None

        if user_found:
            existing_uid = user_found["user_id"]
            print(f"Google Sign-In: Existing user {email}")
            updated = database.update_user(
                existing_uid,
                full_name=name or user_found.get("full_name"),
            )
            if updated["status"] != "success":
                return {"status": "error", "error": updated["error"]}
            return {
                "status": "success",
                "user_id": existing_uid,
                "email": email,
                "name": name or user_found.get("full_name"),
                "is_new_user": False
            }

        # New user - create the account directly in Supabase.
        user_id = f"user_{secrets.token_hex(8)}"
        created = database.create_user(
            user_id=user_id,
            email=email,
            password_hash="",  # No password for Google-only accounts.
            full_name=name,
            created_at_original=datetime.now().isoformat(),
        )
        if created["status"] != "success":
            return {"status": "error", "error": created["error"]}

        print(f"Google Sign-In: New user created {email} (ID: {user_id})")

        return {
            "status": "success",
            "user_id": user_id,
            "email": email,
            "name": name,
            "is_new_user": True
        }

    except Exception as e:
        print(f"Google Auth Error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.post("/gmail/exchange-token")
async def exchange_gmail_token(data: dict):
    """Exchange authorization code for access tokens"""
    try:
        code = data.get("code")
        redirect_uri = data.get("redirect_uri", "http://localhost:8000/auth/callback")
        
        # Exchange code for tokens with Google
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        }
        
        response = requests.post(token_url, data=token_data)
        token_response = response.json()
        
        if "access_token" in token_response:
            access_token = token_response["access_token"]
            refresh_token = token_response.get("refresh_token", "")
            expires_in = token_response.get("expires_in", 3600)
            
            # Get user email
            user_info_url = "https://www.googleapis.com/oauth2/v2/userinfo"
            headers = {"Authorization": f"Bearer {access_token}"}
            user_response = requests.get(user_info_url, headers=headers)
            user_data = user_response.json()
            email = user_data.get("email", "")
            
            print(f"Gmail connected: {email}")
            
            return {
                "status": "success",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "email": email,
                "expiry": expires_in
            }
        else:
            print(f"Token exchange failed: {token_response}")
            return {"status": "error", "error": token_response.get("error_description", "Token exchange failed")}
            
    except Exception as e:
        print(f"Gmail Token Exchange Error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.post("/gmail/verify-token")
async def verify_gmail_token(data: dict):
    """Verify if access token is still valid"""
    try:
        access_token = data.get("access_token")
        
        # Check token with Google
        token_info_url = f"https://oauth2.googleapis.com/tokeninfo?access_token={access_token}"
        response = requests.get(token_info_url)
        
        if response.status_code == 200:
            return {"valid": True}
        else:
            return {"valid": False}
            
    except Exception as e:
        print(f"Token Verify Error: {str(e)}")
        return {"valid": False, "error": str(e)}


@app.post("/gmail/refresh-token")
async def refresh_gmail_token(data: dict):
    """Refresh expired access token"""
    try:
        refresh_token = data.get("refresh_token")
        
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "refresh_token": refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token"
        }
        
        response = requests.post(token_url, data=token_data)
        token_response = response.json()
        
        if "access_token" in token_response:
            return {
                "status": "success",
                "access_token": token_response["access_token"]
            }
        else:
            return {"status": "error", "error": "Token refresh failed"}
            
    except Exception as e:
        print(f"Token Refresh Error: {str(e)}")
        return {"status": "error", "error": str(e)}


# ============================================
# SERVER-SIDE AUTO-SYNC (no browser token needed)
# ============================================
# Previously the ONLY way to pull fresh bank emails was a live browser access
# token, which expires hourly - so data only updated when the user reconnected
# Gmail. By persisting the refresh token on the backend, the server can mint its
# own access token and run the sync on demand. Single responsibility per
# function (Swap-and-Plug): store creds, refresh a token, run one sync.

# How often the server is allowed to auto-sync a user (throttle shield).
SERVER_SYNC_COOLDOWN_SECONDS = 3 * 60  # 3 minutes


@app.post("/gmail/store-credentials")
async def store_gmail_credentials(data: dict):
    """Persist a user's Gmail refresh token so the backend can auto-sync."""
    try:
        user_id = data.get("user_id")
        refresh_token = data.get("refresh_token")
        if not user_id or not refresh_token:
            return {"status": "error", "data": None, "error": "user_id and refresh_token are required"}

        saved = database.save_gmail_credentials(
            user_id=user_id,
            refresh_token=refresh_token,
            access_token=data.get("access_token"),
            token_expiry=data.get("token_expiry"),
            sender_domains=data.get("sender_domains"),
        )
        if saved["status"] != "success":
            return {"status": "error", "data": None, "error": saved["error"]}
        print(f"[Server Sync] Stored refresh token for {user_id}")
        return {"status": "success", "data": {"user_id": user_id}, "error": None}
    except Exception as e:
        print(f"[Server Sync] store-credentials error: {e}")
        return {"status": "error", "data": None, "error": str(e)}


def _get_stored_refresh_token(user_id):
    """Return the stored refresh token for a user, or None."""
    try:
        res = database.get_gmail_credentials(user_id)
        if res["status"] == "success" and res["data"]:
            return res["data"].get("refresh_token")
    except Exception as e:
        print(f"[Server Sync] Could not read credentials for {user_id}: {e}")
    return None


def _mint_access_token(refresh_token):
    """Exchange a refresh token for a fresh access token (server-side)."""
    try:
        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "refresh_token": refresh_token,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        body = resp.json()
        if "access_token" in body:
            return body["access_token"]
        print(f"[Server Sync] Token mint failed: {body}")
    except Exception as e:
        print(f"[Server Sync] Token mint error: {e}")
    return None


def _server_sync_cooldown_elapsed(user_id):
    """True if enough time has passed since the last server-side sync."""
    try:
        res = sync_state_collection.get(ids=[f"serversync_{user_id}"])
        if res["ids"]:
            last = int((res["metadatas"][0] or {}).get("last_server_sync_at", 0))
            return (int(datetime.now().timestamp()) - last) >= SERVER_SYNC_COOLDOWN_SECONDS
    except Exception:
        pass
    return True


def _mark_server_sync(user_id):
    sid = f"serversync_{user_id}"
    meta = {"user_id": user_id, "last_server_sync_at": int(datetime.now().timestamp())}
    try:
        existing = sync_state_collection.get(ids=[sid])
        if existing["ids"]:
            sync_state_collection.update(ids=[sid], metadatas=[meta])
        else:
            sync_state_collection.add(ids=[sid], documents=[f"server sync stamp {user_id}"], metadatas=[meta])
    except Exception as e:
        print(f"[Server Sync] Could not stamp server sync for {user_id}: {e}")


async def run_server_side_gmail_sync(user_id, force=False):
    """
    Pull fresh bank emails for a user entirely server-side: mint an access token
    from the stored refresh token, fetch (delta), and merge into storage. Returns
    a small status dict. Throttled unless force=True.
    """
    if not force and not _server_sync_cooldown_elapsed(user_id):
        return {"synced": False, "reason": "cooldown"}

    refresh_token = _get_stored_refresh_token(user_id)
    if not refresh_token:
        return {"synced": False, "reason": "no_credentials"}

    # Stamp immediately so concurrent dashboard loads don't all trigger a sync.
    _mark_server_sync(user_id)

    access_token = _mint_access_token(refresh_token)
    if not access_token:
        return {"synced": False, "reason": "token_mint_failed"}

    try:
        fetched = await fetch_gmail_emails(
            {"access_token": access_token, "user_id": user_id, "full_resync": force}
        )
        if fetched.get("status") != "success":
            return {"synced": False, "reason": fetched.get("error", "fetch_failed")}
        await sync_gmail_data(
            {"user_id": user_id, "emails": fetched.get("emails", []), "stats": fetched.get("stats", {})}
        )
        return {"synced": True, "count": len(fetched.get("emails", []))}
    except Exception as e:
        print(f"[Server Sync] run_server_side_gmail_sync error for {user_id}: {e}")
        return {"synced": False, "reason": str(e)}


_active_bg_syncs = set()
_active_bg_syncs_lock = threading.Lock()


def spawn_background_sync(user_id, force=False):
    """Run the (blocking) server-side Gmail sync on a dedicated daemon thread.

    run_server_side_gmail_sync is async, but internally it makes BLOCKING network
    calls (refresh-token mint via requests.post, Gmail fetch) plus synchronous
    ChromaDB writes. Scheduling it with asyncio.create_task() ran it on the SAME
    single event loop that serves every HTTP request, so while a sync was in
    flight the whole server was frozen - that is why /get-chats hung on
    "Loading chats..." and the dashboard sat at 40s+. Running it on a separate
    daemon thread (with its own event loop via asyncio.run) keeps that work
    entirely off the request path. The in-flight guard stops overlapping dashboard
    loads from stacking duplicate syncs for the same user.
    """
    with _active_bg_syncs_lock:
        if user_id in _active_bg_syncs:
            return
        _active_bg_syncs.add(user_id)

    def _runner():
        try:
            asyncio.run(run_server_side_gmail_sync(user_id, force=force))
        except Exception as e:
            print(f"[Server Sync] background thread error for {user_id}: {e}")
        finally:
            with _active_bg_syncs_lock:
                _active_bg_syncs.discard(user_id)

    threading.Thread(target=_runner, daemon=True, name=f"gmailsync-{user_id}").start()


async def maybe_autosync_all_users(force=False):
    """Run a throttled server-side sync for every user with stored credentials."""
    try:
        creds = database.get_all_gmail_credentials()
        for row in (creds.get("data") or []):
            uid = (row or {}).get("user_id")
            if uid:
                await run_server_side_gmail_sync(uid, force=force)
    except Exception as e:
        print(f"[Server Sync] maybe_autosync_all_users error: {e}")


@app.post("/gmail/server-sync")
async def trigger_server_sync(data: dict):
    """Manual endpoint: force a server-side sync for the given user."""
    user_id = data.get("user_id", "default")
    result = await run_server_side_gmail_sync(user_id, force=True)
    return {"status": "success", "data": result, "error": None}


@app.get("/gmail/status/{user_id}")
async def gmail_connection_status(user_id: str):
    """Report whether a user's Gmail is connected, reading creds from Supabase.
    'Connected' means we hold a refresh token the backend can auto-sync with."""
    try:
        res = database.get_gmail_credentials(user_id)
        if res["status"] != "success":
            return {"status": "error", "data": None, "error": res["error"]}
        cred = res["data"]
        connected = bool(cred and cred.get("refresh_token"))
        return {
            "status": "success",
            "data": {
                "connected": connected,
                "sender_domains": (cred or {}).get("sender_domains") or [],
                "token_expiry": (cred or {}).get("token_expiry"),
                "updated_at": (cred or {}).get("updated_at"),
            },
            "error": None,
        }
    except Exception as e:
        print(f"[Server Sync] gmail status error for {user_id}: {e}")
        return {"status": "error", "data": None, "error": str(e)}


# ============================================
# DELTA SYNC BOOKMARK ("The Bookmark Method")
# ============================================
# Single responsibility: read/write the per-user "last_synced_timestamp" so the
# Gmail fetch only ever processes emails that arrived AFTER the last sync. This
# is what stops the single-threaded server from re-scanning all 554 emails (and
# re-running an AI classification per email) on every sync, which was freezing
# the UI.

# How far back a brand-new user (no bookmark yet) is scanned on first sync.
NEW_USER_LOOKBACK_DAYS = 7
# Wider window used for a full (re)build when the user has NO stored emails,
# e.g. first-ever sync or recovering after the blob was emptied.
FULL_LOOKBACK_DAYS = 90
# Safety overlap subtracted from the bookmark on each delta sync so emails near
# the previous boundary are never permanently skipped (dedup handles overlap).
BOOKMARK_OVERLAP_SECONDS = 24 * 60 * 60  # 24 hours


def get_last_synced_timestamp(user_id="default"):
    """Return the user's last synced time as an epoch int (seconds), or None."""
    try:
        res = sync_state_collection.get(ids=[f"sync_{user_id}"])
        if res["ids"]:
            ts = (res["metadatas"][0] or {}).get("last_synced_timestamp")
            if ts:
                return int(ts)
    except Exception as e:
        print(f"[Delta Sync] Could not read bookmark for {user_id}: {e}")
    return None


def set_last_synced_timestamp(user_id, epoch_seconds):
    """Persist the user's bookmark (epoch seconds). Idempotent upsert."""
    try:
        sid = f"sync_{user_id}"
        meta = {
            "user_id": user_id,
            "last_synced_timestamp": int(epoch_seconds),
            "updated_at": datetime.now().isoformat(),
        }
        existing = sync_state_collection.get(ids=[sid])
        if existing["ids"]:
            sync_state_collection.update(ids=[sid], metadatas=[meta])
        else:
            sync_state_collection.add(
                ids=[sid],
                documents=[f"Delta sync bookmark for {user_id}"],
                metadatas=[meta],
            )
    except Exception as e:
        print(f"[Delta Sync] Could not write bookmark for {user_id}: {e}")


@app.post("/gmail/fetch-emails")
async def fetch_gmail_emails(data: dict):
    """Fetch financial emails from Gmail using an incremental delta sync."""
    try:
        access_token = data.get("access_token")
        user_id = data.get("user_id", "default")
        # When True (manual "Refresh Data"), ignore the bookmark and re-pull a
        # full recent window so the user always gets complete, fresh data.
        full_resync = bool(data.get("full_resync", False))
        print(f"[v0] fetch-emails called: user_id={user_id} full_resync={full_resync} (DELTA-SYNC-BUILD)")
        
        if not access_token:
            return {"status": "error", "error": "No access token provided"}
        
        headers = {"Authorization": f"Bearer {access_token}"}
        
        # First, verify the token is valid with a simple test call.
        # A short timeout guarantees a dead/slow Gmail call can never hang the
        # single-threaded server (defensive: assume the network will fail).
        test_url = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
        test_response = requests.get(test_url, headers=headers, timeout=10)
        
        if test_response.status_code == 401:
            # Token expired or invalid - tell frontend to re-authenticate
            return {
                "status": "error", 
                "error": "token_expired",
                "message": "Your Gmail session has expired. Please reconnect your Gmail account.",
                "requires_reauth": True
            }
        
        # ============================================
        # STEP 1: READ THE BOOKMARK (last_synced_timestamp)
        # ============================================
        from datetime import datetime as _dt

        # Self-heal: check whether this user actually has stored emails. If the
        # stored blob is empty/missing, the bookmark is meaningless (or stale
        # after a wipe), so we ignore it and do a FULL re-pull. This recovers
        # data automatically without the user reconnecting Gmail.
        has_stored_emails = False
        try:
            stored = gmail_data_collection.get(ids=[f"gmail_{user_id}"])
            if stored["documents"]:
                stored_doc = json.loads(stored["documents"][0])
                has_stored_emails = len(stored_doc.get("emails", [])) > 0
        except Exception:
            has_stored_emails = False

        bookmark = get_last_synced_timestamp(user_id)

        if full_resync:
            # Manual "Refresh Data": deliberately ignore the bookmark and re-pull
            # the full recent window. The merge in /sync-gmail-data dedups by id,
            # so already-stored emails are harmless and only genuinely new ones
            # change anything. This is what guarantees a manual refresh shows the
            # latest balance/transactions instead of the last bookmarked state.
            after_epoch = int((datetime.now() - timedelta(days=FULL_LOOKBACK_DAYS)).timestamp())
            print(
                f"INFO: [Delta Sync] FULL RESYNC requested for {user_id}. Re-pulling "
                f"the last {FULL_LOOKBACK_DAYS} days (after epoch {after_epoch}), "
                f"ignoring the bookmark."
            )
        elif not has_stored_emails:
            # Recovery / first-ever sync: pull a wide history window and ignore
            # any stale bookmark so we rebuild the full picture.
            after_epoch = int((datetime.now() - timedelta(days=FULL_LOOKBACK_DAYS)).timestamp())
            print(
                f"INFO: [Delta Sync] No stored emails for {user_id}. Doing a FULL "
                f"re-pull of the last {FULL_LOOKBACK_DAYS} days (after epoch {after_epoch}), "
                f"ignoring any stale bookmark."
            )
        elif bookmark:
            # Apply a safety overlap: scan a little BEFORE the bookmark so emails
            # that landed near the previous boundary are never permanently
            # skipped. The merge in /sync-gmail-data dedups by id, so re-scanning
            # this overlap is harmless.
            after_epoch = bookmark - BOOKMARK_OVERLAP_SECONDS
            print(
                f"INFO: [Delta Sync] Bookmark found. Scanning emails received after: "
                f"{_dt.fromtimestamp(after_epoch).isoformat()} (epoch {after_epoch}, "
                f"includes a {BOOKMARK_OVERLAP_SECONDS // 3600}h safety overlap)"
            )
        else:
            # Has emails but no bookmark: scan a safe recent baseline only.
            after_epoch = int((datetime.now() - timedelta(days=NEW_USER_LOOKBACK_DAYS)).timestamp())
            print(
                f"INFO: [Delta Sync] No bookmark found. Falling back to "
                f"last {NEW_USER_LOOKBACK_DAYS} days (after epoch {after_epoch})."
            )

        # ============================================
        # STEP 2: BUILD DELTA-FILTERED GMAIL QUERIES
        # ============================================
        # Every query is constrained with `after:{epoch}` so Google returns ONLY
        # messages newer than the bookmark. With a recent bookmark this is a tiny
        # result set (often zero), which is what keeps the sync fast.
        after_clause = f"after:{after_epoch}"
        search_queries = [
            # Targeted financial keyword sweep, delta-filtered.
            f"{after_clause} (debit OR credit OR debited OR credited OR transaction OR transfer OR alert OR payment OR receipt OR invoice OR account)",

            # Direct bank sender searches (high confidence), delta-filtered.
            f"{after_clause} from:wemaalert@wemabank.com",
            f"{after_clause} from:wemabank.com",
            f"{after_clause} from:alat.ng",
            f"{after_clause} from:firstbanknigeria.com",
            f"{after_clause} from:gtbank.com",
            f"{after_clause} from:zenithbank.com",
            f"{after_clause} from:accessbankplc.com",
            f"{after_clause} from:sterlingbankng.com",
            f"{after_clause} from:fidelitybank.ng",
            f"{after_clause} from:fcmb.com",
            f"{after_clause} from:unionbankng.com",
            f"{after_clause} from:ecobank",
            f"{after_clause} from:stanbicibtc",
            f"{after_clause} from:aborogu",
        ]
        
        all_messages = []
        emails_scanned = 0
        seen_ids = set()  # Track seen message IDs to avoid duplicates
        # Track the newest email we actually process so we can move the bookmark.
        max_internal_date_ms = (after_epoch * 1000)
        
        for query in search_queries:
            # Search for messages - delta-filtered, so result sets stay small.
            search_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={query}&maxResults=50"
            response = requests.get(search_url, headers=headers, timeout=15)
            
            if response.status_code != 200:
                print(f"Gmail API Error: {response.text}")
                continue
            
            search_data = response.json()
            messages = search_data.get("messages", [])
            emails_scanned += len(messages)
            
            # Get details for each NEW message.
            for msg in messages[:25]:
                if msg['id'] in seen_ids:
                    continue
                seen_ids.add(msg['id'])
                
                # Fetch FULL message (including body) for bank alerts to extract amounts
                msg_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=full"
                msg_response = requests.get(msg_url, headers=headers, timeout=15)
                
                if msg_response.status_code == 200:
                    msg_data = msg_response.json()

                    # Capture Gmail's internalDate (epoch ms) for the bookmark,
                    # regardless of whether the email is classified as financial.
                    try:
                        internal_ms = int(msg_data.get("internalDate", 0))
                        if internal_ms > max_internal_date_ms:
                            max_internal_date_ms = internal_ms
                    except (TypeError, ValueError):
                        pass

                    parsed_email = parse_gmail_message_full(msg_data)
                    if parsed_email and parsed_email['id'] not in [e.get('id') for e in all_messages]:
                        all_messages.append(parsed_email)
        
        # Remove duplicates by a COMPOSITE key, NOT by subject.
        # FirstBank sends multiple distinct transactions that all share the exact
        # same subject ("FirstBank Alert on Your Account - Debit"). Deduping by
        # subject wrongly collapsed a 3-email thread down to a single transaction.
        # The Gmail message id is already unique per email; we add amount/date/
        # narration so we still drop true re-sends while keeping every real txn.
        seen_keys = set()
        unique_emails = []
        for email in all_messages:
            dedup_key = (
                email.get('id'),
                str(email.get('amount', '')),
                str(email.get('date', '')),
                str(email.get('narration', ''))[:60],
            )
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                unique_emails.append(email)
        
        # Sort by date (newest first)
        unique_emails.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        # Limit to 30 most recent (increased from 15)
        unique_emails = unique_emails[:30]
        
        # Calculate stats
        bills_count = sum(1 for e in unique_emails if e.get('type') == 'bill')
        
        print(f"Gmail fetch complete: {emails_scanned} scanned, {len(unique_emails)} financial emails found")

        # ============================================
        # STEP 3: MOVE THE BOOKMARK FORWARD
        # ============================================
        # Advance the bookmark to the newest email we processed so the NEXT sync
        # starts exactly where this one ended. We only ever move it forward
        # (never backward) to stay safe if a query returned older messages.
        from datetime import datetime as _dt
        new_bookmark_epoch = max_internal_date_ms // 1000
        if new_bookmark_epoch > after_epoch:
            set_last_synced_timestamp(user_id, new_bookmark_epoch)
            print(
                f"INFO: [Delta Sync] Processed {len(unique_emails)} new emails. "
                f"Moving bookmark forward to: {_dt.fromtimestamp(new_bookmark_epoch).isoformat()} "
                f"(epoch {new_bookmark_epoch})"
            )
        else:
            print(
                f"INFO: [Delta Sync] Processed {len(unique_emails)} new emails. "
                f"Bookmark unchanged (no emails newer than the current bookmark)."
            )
        
        return {
            "status": "success",
            "emails": unique_emails,
            "stats": {
                "emailsScanned": emails_scanned,
                "transactionsFound": len(unique_emails),
                "billsDetected": bills_count
            }
        }
        
    except Exception as e:
        print(f"Gmail Fetch Error: {str(e)}")
        return {"status": "error", "error": str(e)}


def get_email_body(payload):
    """Extract email body from Gmail payload (handles multipart)"""
    import base64
    
    body = ""
    
    if "body" in payload and payload["body"].get("data"):
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    elif "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                break
            elif part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                # Fallback to HTML if no plain text
                html_body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                # Strip HTML tags for basic text extraction
                import re
                body = re.sub(r'<[^>]+>', ' ', html_body)
                body = re.sub(r'\s+', ' ', body).strip()
            elif "parts" in part:
                # Nested multipart
                for subpart in part["parts"]:
                    if subpart.get("body", {}).get("data"):
                        body = base64.urlsafe_b64decode(subpart["body"]["data"]).decode("utf-8", errors="ignore")
                        break
    
    return body[:5000]  # Limit to 5000 chars


def parse_gmail_message_full(msg_data):
    """Parse Gmail message with FULL body content for amount extraction"""
    try:
        headers = msg_data.get("payload", {}).get("headers", [])

        # Gmail's internalDate is the precise received time (epoch milliseconds).
        # We persist it on every email so the dashboard can order transactions
        # WITHIN the same calendar day - critical for picking the true latest
        # balance when several alerts share one date.
        try:
            internal_date_ms = int(msg_data.get("internalDate", 0))
        except (TypeError, ValueError):
            internal_date_ms = 0
        
        sender = ""
        sender_email = ""
        subject = ""
        date_str = ""
        
        for header in headers:
            name = header.get("name", "").lower()
            value = header.get("value", "")
            
            if name == "from":
                # Extract both sender name and email
                if "<" in value:
                    sender = value.split("<")[0].strip().strip('"')
                    sender_email = value.split("<")[1].replace(">", "").strip()
                else:
                    sender = value.split("@")[0]
                    sender_email = value
            elif name == "subject":
                subject = value
            elif name == "date":
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(value)
                    date_str = dt.strftime("%Y-%m-%d")
                except:
                    date_str = value[:10] if len(value) >= 10 else value
        
        if not sender or not subject:
            return None
        
        # Get full email body
        body = get_email_body(msg_data.get("payload", {}))
        
        # Check for attachments (for Lane B detection)
        has_attachment = False
        payload = msg_data.get("payload", {})
        if "parts" in payload:
            for part in payload["parts"]:
                if part.get("filename") and part.get("filename").lower().endswith(('.pdf', '.doc', '.docx')):
                    has_attachment = True
                    break
        
        # Use Smart Mailroom Clerk classification (3-Lane System)
        classification = ai_classify_email(sender_email, subject, body[:500], has_attachment)
        
        # LANE C: Marketing Trash - SKIP ENTIRELY
        if classification.get("skip", False):
            return None  # Do not process marketing/non-financial emails
        
        # LANE A: Bank Alert
        is_bank = classification["type"] == "bank_alert"
        
        # LANE B: Vendor Receipt (or LANE A bank)
        if not classification["is_financial"]:
            return None  # Extra safety check
        
        # Determine email type based on lane
        if is_bank:
            email_type = "bank_alert"
        elif classification["type"] == "vendor_receipt":
            email_type = "receipt"
        else:
            email_type = categorize_email(sender, subject)
        
        # Use bank name from classification (only for verified banks)
        detected_bank = classification.get("bank_name")
        
        # Extract financial data based on type
        if is_bank:
            bank_data = extract_bank_alert_data(body, subject, sender_email)
            amount = bank_data.get("amount")
            
            # === STEP B: TRUE-UP CHECK ===
            # If this is a debit, check if it matches any pending currency conversion
            true_up_info = None
            if bank_data.get("transaction_type") == "debit" and amount:
                try:
                    matching = find_matching_pending_conversion(float(amount), date_str)
                    if matching:
                        # Found a match! True-up the estimate with actual bank amount
                        true_up_conversion(matching["id"], float(amount), msg_data.get("id", ""))
                        true_up_info = {
                            "matched_vendor": matching["metadata"].get("vendor"),
                            "original_currency": matching["metadata"].get("original_currency"),
                            "original_amount": matching["metadata"].get("original_amount"),
                            "estimated_ngn": matching["metadata"].get("estimated_ngn"),
                            "actual_ngn": amount,
                            "difference": float(amount) - float(matching["metadata"].get("estimated_ngn", 0)),
                            "confidence": matching["match_confidence"]
                        }
                        print(f"Bank True-Up: {matching['metadata'].get('vendor')} - Est ₦{matching['metadata'].get('estimated_ngn')} → Actual ₦{amount}")
                except Exception as e:
                    print(f"True-up check error: {e}")
            
            return {
                "id": msg_data.get("id"),
                "sender": sender,
                "sender_email": sender_email,
                "subject": subject[:60] + "..." if len(subject) > 60 else subject,
                "date": date_str,
                "internal_date_ms": internal_date_ms,
                "type": email_type,
                "amount": bank_data.get("amount"),
                "currency": bank_data.get("currency", "NGN"),
                "transaction_type": bank_data.get("transaction_type"),
                "balance": bank_data.get("balance"),
                "narration": bank_data.get("narration"),
                "category": "Banking",
                "is_bank_alert": True,
                "bank_name": detected_bank or bank_data.get("bank_name"),
                "confidence": classification["confidence"],
                "needs_review": bank_data.get("needs_review", False),
                "true_up": true_up_info  # Will contain matched foreign transaction info if found
            }
        else:
            # Regular receipt/transaction - try to extract amount from body
            amount = extract_amount_from_body(body, subject)
            category = get_category(sender, subject)
            
            # Detect currency from body/subject
            currency = detect_currency(body, subject, sender_email)
            
            return {
                "id": msg_data.get("id"),
                "sender": sender,
                "sender_email": sender_email,
                "subject": subject[:60] + "..." if len(subject) > 60 else subject,
                "date": date_str,
                "internal_date_ms": internal_date_ms,
                "type": email_type,
                "amount": amount,
                "currency": currency,
                "category": category,
                "is_bank_alert": False,
                "bank_name": detected_bank,
                "confidence": classification["confidence"]
            }
        
    except Exception as e:
        print(f"Parse error: {str(e)}")
        return None


# ============================================
# AI-POWERED EMAIL CLASSIFICATION MODEL
# ============================================
# This uses Gemini AI with the bank directory as training context
# to intelligently classify ANY email as financial or not

# Bank Directory - Used as MODEL/CONTEXT for AI classification
# AI learns patterns from this and can identify similar emails
BANK_DIRECTORY = {
    # Nigerian Banks
    "nigeria": {
        "banks": [
            {"name": "WEMA Bank", "senders": ["wemaalert@wemabank.com", "wemabank.com", "alat.ng", "noreply.alat.ng", "info@noreply.alat.ng"], 
             "patterns": ["WEMA ALERT", "Has Been Credited", "Has Been Debited", "Transaction Details", "WEMA BANK", "ALAT"]},
            {"name": "FirstBank", "senders": ["firstalert@firstbanknigeria.com", "firstbank@firstbanknigeria.com", "firstbanknigeria.com"],
             "patterns": ["FirstBank Alert", "transaction notification", "Cleared Balance", "Transaction Details", "FirstBank"]},
            {"name": "GTBank", "senders": ["gtbank.com", "gtbplc.com"],
             "patterns": ["GTBank", "transaction alert", "debit", "credit"]},
            {"name": "Zenith Bank", "senders": ["zenithbank.com"],
             "patterns": ["Zenith", "transaction", "alert"]},
            {"name": "Access Bank", "senders": ["accessbankplc.com"],
             "patterns": ["Access Bank", "transaction", "notification"]},
            {"name": "UBA", "senders": ["ubagroup.com", "ubaalert"],
             "patterns": ["UBA", "transaction", "alert"]},
            {"name": "Sterling Bank", "senders": ["sterlingbankng.com"],
             "patterns": ["Sterling", "transaction"]},
            {"name": "Fidelity Bank", "senders": ["fidelitybank.ng"],
             "patterns": ["Fidelity", "alert"]},
            {"name": "FCMB", "senders": ["fcmb.com"],
             "patterns": ["FCMB", "transaction"]},
            {"name": "Polaris Bank", "senders": ["polarisbanklimited.com"],
             "patterns": ["Polaris", "transaction"]},
            {"name": "Union Bank", "senders": ["unionbankng.com"],
             "patterns": ["Union Bank", "transaction"]},
            {"name": "Stanbic IBTC", "senders": ["stanbicibtc.com"],
             "patterns": ["Stanbic", "IBTC", "transaction"]},
            {"name": "Ecobank", "senders": ["ecobank.com"],
             "patterns": ["Ecobank", "transaction"]},
        ],
        "currency": "NGN",
        "symbol": "₦"
    },
    # Add more countries as needed
    "payment_services": [
        {"name": "OPay", "senders": ["opay.com"], "patterns": ["OPay", "payment", "transfer"]},
        {"name": "PalmPay", "senders": ["palmpay.com"], "patterns": ["PalmPay", "payment"]},
        {"name": "Flutterwave", "senders": ["flutterwave.com"], "patterns": ["Flutterwave", "payment"]},
        {"name": "Paystack", "senders": ["paystack.com"], "patterns": ["Paystack", "payment", "receipt"]},
    ]
}

# ============================================
# SMART MAILROOM CLERK - 3 LANE SYSTEM
# ============================================
# Lane A: Official Bank Alerts (verified sender domains)
# Lane B: Vendor Receipts & Invoices (PDF attachments or transactional subjects)
# Lane C: Marketing Trash Can (promotional emails - SKIP ENTIRELY)

# Marketing/Promotional sender blacklist - NEVER process these
MARKETING_BLACKLIST = [
    # Educational platforms (send promotions, not bills)
    "codecademy", "coursera", "udemy", "udacity", "skillshare", "masterclass",
    "linkedin.com", "edx.org", "khanacademy", "pluralsight", "datacamp",
    # Social media
    "discord", "twitter", "facebook", "instagram", "tiktok", "snapchat",
    "pinterest", "reddit", "tumblr", "whatsapp",
    # News & Marketing
    "newsletter", "mailchimp", "sendgrid", "substack", "medium.com",
    "hubspot", "constantcontact", "campaignmonitor",
    # Job sites
    "indeed", "glassdoor", "ziprecruiter", "monster.com",
    # General promotions
    "promo", "marketing", "noreply@", "no-reply@", "donotreply",
    # Tech marketing (not billing)
    "info@aws", "aws-marketing", "google-community", "microsoft-noreply",
    "apple-news", "github-noreply", "notifications@github",
    # African marketing
    "jumia-promo", "konga-deals", "betway", "bet9ja", "sportybet",
]

# Marketing subject keywords - instant rejection
MARKETING_SUBJECT_KEYWORDS = [
    "newsletter", "promo", "% off", "discount", "sale", "offer",
    "join millions", "get started", "don't miss", "limited time",
    "exclusive deal", "free trial", "unsubscribe", "weekly digest",
    "monthly update", "new features", "what's new", "tips for",
    "learn how", "webinar", "invitation to", "you're invited",
    "congratulations", "welcome to", "getting started", "introduction to",
    "password changed", "login from new", "verify your email",
    "confirm your email", "activate your", "update your profile",
]

# Verified bank sender domains - ONLY these can be Lane A
VERIFIED_BANK_DOMAINS = [
    # Nigerian Banks
    "wemabank.com", "wemaalert@wemabank.com", "alat.ng", "noreply.alat.ng",
    "firstbanknigeria.com", "firstalert@firstbanknigeria.com",
    "gtbank.com", "gtbplc.com",
    "zenithbank.com",
    "accessbankplc.com",
    "sterlingbankng.com",
    "fidelitybank.ng",
    "fcmb.com",
    "unionbankng.com",
    "stanbicibtc.com",
    "ecobank.com",
    "ubagroup.com",
    "aborogu",
    # Mauritius Banks
    "mcb.mu", "sbmgroup.mu", "absa.mu",
    # South African Banks  
    "fnb.co.za", "nedbank.co.za", "capitecbank.co.za", "standardbank.co.za",
    # Kenyan Banks
    "equitybank.co.ke", "kcbgroup.com", "co-opbank.co.ke",
]

# Receipt/Invoice subject keywords - Lane B triggers
RECEIPT_SUBJECT_KEYWORDS = [
    "receipt", "invoice", "bill", "payment confirmation", "order #",
    "order confirmation", "purchase confirmation", "payment received",
    "transaction successful", "payment successful", "billing statement",
    "your order", "subscription charged", "subscription renewed",
    "charge from", "charged to your", "payment to", "paid to",
]

# Verified vendor domains that send real receipts/invoices
VERIFIED_VENDOR_DOMAINS = [
    # Cloud Services (send real invoices)
    "billing.aws.amazon.com", "payments.google.com", "azure.microsoft.com",
    "billing@vercel.com", "billing@stripe.com", "billing@digitalocean.com",
    # Software subscriptions
    "receipts@canva.com", "billing@notion.so", "billing@figma.com",
    "billing@openai.com", "receipts@adobe.com", "billing@zoom.us",
    # Payment processors
    "paypal.com", "stripe.com", "flutterwave.com", "paystack.com",
    "interswitch.com", "opay.com", "palmpay.com",
    # E-commerce (order confirmations)
    "orders@amazon", "order@jumia", "orders@konga",
]


def smart_mailroom_classify(sender_email, subject, body_preview="", has_attachment=False):
    """
    SMART MAILROOM CLERK - 3 Lane Classification System
    
    Lane A: Official Bank Alerts → Extract balance/transaction
    Lane B: Vendor Receipts/Invoices → Extract as expense
    Lane C: Marketing Trash → SKIP ENTIRELY (return None)
    
    Returns: {
        "lane": "A" | "B" | "C",
        "is_financial": bool,
        "confidence": float,
        "type": str,
        "bank_name": str | None,
        "skip": bool  # If True, don't process this email at all
    }
    """
    sender_lower = sender_email.lower()
    subject_lower = subject.lower()
    
    # ========== LANE C CHECK FIRST: Marketing Trash Can ==========
    # Check sender blacklist
    for blacklisted in MARKETING_BLACKLIST:
        if blacklisted in sender_lower:
            return {
                "lane": "C",
                "is_financial": False,
                "confidence": 0.99,
                "type": "marketing_trash",
                "bank_name": None,
                "skip": True  # DO NOT PROCESS
            }
    
    # Check subject for marketing keywords
    marketing_keyword_count = sum(1 for kw in MARKETING_SUBJECT_KEYWORDS if kw in subject_lower)
    if marketing_keyword_count >= 2:
        return {
            "lane": "C",
            "is_financial": False,
            "confidence": 0.95,
            "type": "marketing_trash",
            "bank_name": None,
            "skip": True
        }
    
    # ========== LANE A CHECK: Official Bank Alerts ==========
    # STRICT: Only if sender domain EXACTLY matches verified bank
    detected_bank = None
    for bank_domain in VERIFIED_BANK_DOMAINS:
        if bank_domain in sender_lower:
            # Find the bank name from BANK_DIRECTORY
            for country, data in BANK_DIRECTORY.items():
                if isinstance(data, dict) and "banks" in data:
                    for bank in data["banks"]:
                        if any(s in sender_lower for s in bank["senders"]):
                            detected_bank = bank["name"]
                            break
            
            return {
                "lane": "A",
                "is_financial": True,
                "confidence": 0.99,
                "type": "bank_alert",
                "bank_name": detected_bank or "Unknown Bank",
                "skip": False
            }
    
    # ========== LANE B CHECK: Vendor Receipts & Invoices ==========
    # Check 1: Has PDF attachment (strong indicator of invoice)
    if has_attachment:
        # Check if subject suggests it's a receipt/invoice
        if any(kw in subject_lower for kw in RECEIPT_SUBJECT_KEYWORDS):
            return {
                "lane": "B",
                "is_financial": True,
                "confidence": 0.90,
                "type": "vendor_receipt",
                "bank_name": None,
                "skip": False
            }
    
    # Check 2: Subject contains strong receipt/invoice keywords
    receipt_keyword_count = sum(1 for kw in RECEIPT_SUBJECT_KEYWORDS if kw in subject_lower)
    if receipt_keyword_count >= 1:
        # Verify it's not marketing disguised as receipt
        if marketing_keyword_count == 0:
            return {
                "lane": "B",
                "is_financial": True,
                "confidence": 0.85,
                "type": "vendor_receipt",
                "bank_name": None,
                "skip": False
            }
    
    # Check 3: From verified vendor billing domain
    for vendor in VERIFIED_VENDOR_DOMAINS:
        if vendor in sender_lower:
            return {
                "lane": "B",
                "is_financial": True,
                "confidence": 0.90,
                "type": "vendor_receipt",
                "bank_name": None,
                "skip": False
            }
    
    # ========== DEFAULT: Not clearly financial, but not marketing either ==========
    # Don't process, but don't mark as trash (might be personal email)
    return {
        "lane": "C",
        "is_financial": False,
        "confidence": 0.7,
        "type": "non_financial",
        "bank_name": None,
        "skip": True  # Skip but don't trash - just not relevant
    }


def ai_classify_email(sender_email, subject, body_preview="", has_attachment=False):
    """
    WRAPPER: Calls the Smart Mailroom Clerk system.
    Backwards compatible with existing code.
    """
    result = smart_mailroom_classify(sender_email, subject, body_preview, has_attachment)
    
    # Convert to old format for backwards compatibility
    return {
        "is_financial": result["is_financial"],
        "confidence": result["confidence"],
        "type": result["type"],
        "bank_name": result["bank_name"],
        "lane": result["lane"],
        "skip": result["skip"]
    }


def is_bank_alert(sender_email, subject, body=""):
    """
    AI-POWERED: Detect if email is a bank alert from ANY bank worldwide.
    Uses keyword detection + AI fallback for unknown formats.
    """
    sender_lower = sender_email.lower()
    subject_lower = subject.lower()
    combined_text = f"{sender_lower} {subject_lower}".lower()
    
    # Known Nigerian bank sender patterns (explicit detection)
    nigerian_bank_senders = [
        "wemaalert", "wemabank", "firstbank", "firstalert", "gtbank",
        "zenithbank", "zenith", "accessbank", "access", "sterlingbank",
        "fidelitybank", "polarisbank", "fcmb", "unionbank", "stanbic",
        "ecobank", "ubaalert", "ubagroup"
    ]
    
    # Direct match for known banks
    if any(bank in sender_lower for bank in nigerian_bank_senders):
        return True
    
    # Universal bank alert keywords (work for any bank globally)
    bank_keywords_strong = [
        "debit", "credit", "debited", "credited", 
        "transaction alert", "transaction notification",
        "account alert", "bank alert", "banking alert",
        "account debited", "account credited",
        "available balance", "current balance", "cleared balance",
        "withdrawal", "deposit notification",
        "has been credited", "has been debited"  # WEMA format
    ]
    
    # Sender patterns that indicate bank emails
    bank_sender_patterns = [
        "alert@", "alerts@", "notify@", "notification@", "noreply@",
        "transaction@", "banking@", "ebanking@", "info@"
    ]
    
    # Check strong keywords in subject
    has_strong_keyword = any(kw in subject_lower for kw in bank_keywords_strong)
    
    # Check if sender looks like a bank notification
    has_bank_sender = any(pattern in sender_lower for pattern in bank_sender_patterns)
    
    # Check for "bank" in sender domain
    has_bank_in_sender = "bank" in sender_lower
    
    # Decision logic
    if has_strong_keyword and (has_bank_in_sender or has_bank_sender):
        return True
    if has_strong_keyword and ("debit" in subject_lower or "credit" in subject_lower):
        return True
    if "transaction" in subject_lower and "account" in subject_lower:
        return True
    if "your account" in subject_lower and ("debit" in subject_lower or "credit" in subject_lower):
        return True
    # WEMA specific pattern
    if "wema" in combined_text and ("credit" in subject_lower or "debit" in subject_lower):
        return True
    
    return False


# Keep old function name for compatibility
def is_nigerian_bank_alert(sender_email, subject):
    """Backwards compatible wrapper"""
    return is_bank_alert(sender_email, subject)


def extract_bank_alert_data(body, subject, sender_email):
    """
    DETERMINISTIC REGEX ENGINE for bank alerts - NO AI GUESSING.
    
    Banks use rigid HTML templates that never change randomly.
    Using AI to parse structured bank emails is over-engineering.
    
    This function uses keyword-locked regex patterns that ONLY extract
    amounts that immediately follow verified trigger words like "Amount:", "Amt:", etc.
    
    Reference IDs, Session IDs, and Account numbers are NEVER mistaken for amounts.
    """
    import re
    
    result = {
        "amount": None,
        "transaction_type": None,
        "balance": None,
        "narration": None,
        "bank_name": None,
        "currency": "NGN",  # Default to NGN for Nigerian app
        "needs_review": False
    }
    
    # --- STEP 1: Extract bank name from sender email ---
    sender_lower = sender_email.lower()
    
    known_banks = {
        # Nigeria
        "firstbank": "FirstBank", "firstalert": "FirstBank", "gtbank": "GTBank",
        "zenith": "Zenith Bank", "wema": "WEMA Bank", "wemaalert": "WEMA Bank", "alat": "WEMA Bank",
        "access": "Access Bank", "sterling": "Sterling Bank", "fidelity": "Fidelity Bank",
        "polaris": "Polaris Bank", "fcmb": "FCMB", "union": "Union Bank",
        "stanbic": "Stanbic IBTC", "ecobank": "Ecobank", "uba": "UBA", "ubaalert": "UBA",
        # Mauritius
        "mcb": "MCB Bank", "sbm": "SBM Bank", "absa": "Absa Bank",
        "standardbank": "Standard Bank", "afrasia": "AfrAsia Bank",
        # South Africa
        "fnb": "FNB", "nedbank": "Nedbank", "capitec": "Capitec",
        # Kenya
        "equity": "Equity Bank", "kcb": "KCB Bank", "cooperative": "Co-op Bank",
        # Ghana
        "gcb": "GCB Bank", "calbank": "CalBank",
    }
    
    for key, name in known_banks.items():
        if key in sender_lower:
            result["bank_name"] = name
            break
    
    if not result["bank_name"]:
        domain_match = re.search(r'@([^.]+)', sender_email)
        if domain_match:
            domain_name = domain_match.group(1)
            result["bank_name"] = domain_name.replace("alert", "").replace("notify", "").title() + " Bank"
    
    # --- STEP 2: Determine transaction type from subject ---
    subject_lower = subject.lower()
    body_lower = body.lower()
    
    if "debit" in subject_lower or "debited" in subject_lower or "debit" in body_lower[:200]:
        result["transaction_type"] = "debit"
    elif "credit" in subject_lower or "credited" in subject_lower or "credit" in body_lower[:200]:
        result["transaction_type"] = "credit"
    
    # --- STEP 3: DETERMINISTIC AMOUNT EXTRACTION (Keyword-Locked) ---
    # These patterns ONLY match amounts that immediately follow trigger keywords.
    # This prevents Reference IDs, Session IDs, Account numbers from being extracted.
    
    # STRICT amount patterns - keyword MUST precede the number
    strict_amount_patterns = [
        # "Amount: NGN 5,000.00" or "Amount : 5,000.00 NGN" or "Amount:NGN5,000.00"
        r'(?:Amount|Amt|Transaction\s*Amount)[:\s]+(?:NGN|₦|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*\.?[0-9]{0,2})',
        # "NGN 5,000.00 was debited" - currency followed by amount in transactional context
        r'(?:NGN|₦)\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*(?:was|has been|debited|credited)',
        # "5,000.00 NGN" or "5,000.00 DR" - amount followed by currency/type marker
        r'([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*(?:NGN|DR|CR)\b',
        # "debited with NGN 5,000.00" or "credited with 5,000.00"
        r'(?:debited|credited)\s*(?:with)?\s*(?:NGN|₦|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*\.?[0-9]{0,2})',
        # "sum of NGN5,000.00" or "sum of 5,000.00"
        r'sum\s*of\s*(?:NGN|₦|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*\.?[0-9]{0,2})',
        # "Total: 5,000.00" or "Total Amount: 5,000.00"
        r'(?:Total|Total\s*Amount)[:\s]+(?:NGN|₦|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*\.?[0-9]{0,2})',
    ]
    
    for pattern in strict_amount_patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            amount_str = match.group(1).replace(",", "")
            try:
                amount = float(amount_str)
                # SANITY CHECK: Reject impossible amounts
                if amount > 0 and amount < 100_000_000:  # Max 100 million NGN
                    result["amount"] = amount
                    break
                else:
                    # Amount too large - likely a reference ID
                    result["needs_review"] = True
            except:
                pass
    
    # --- STEP 4: Additional sanity checks for extracted amount ---
    if result["amount"]:
        # Check if the "amount" looks like a reference number (no decimal, 8+ digits)
        amount_str = str(result["amount"])
        if result["amount"] > 50_000_000:  # 50 million NGN ceiling for personal accounts
            result["needs_review"] = True
            # Try to find a smaller, more reasonable amount
            smaller_match = re.search(r'([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})', body)
            if smaller_match:
                try:
                    smaller_amount = float(smaller_match.group(1).replace(",", ""))
                    if smaller_amount < 50_000_000:
                        result["amount"] = smaller_amount
                        result["needs_review"] = False
                except:
                    pass
    
    # --- STEP 5: DETERMINISTIC BALANCE EXTRACTION ---
    strict_balance_patterns = [
        # "Balance: NGN 191,425.43" or "Balance : 191,425.43 CR"
        r'(?:Balance|Cleared\s*Balance|Available\s*Balance|Current\s*Balance)[:\s]+(?:NGN|₦|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*\.?[0-9]{0,2})',
        # "Current Balance as at 14-05-2026 13:45:40 : 307,775.68 NGN"
        r'Current\s*Balance\s*(?:as\s*at)?[^:]*:\s*([0-9]{1,3}(?:,[0-9]{3})*\.?[0-9]{0,2})\s*(?:NGN)?',
        # "balance is NGN 5,000.00"
        r'balance\s*(?:is|of)?\s*(?:NGN|₦|N)?\s*([0-9]{1,3}(?:,[0-9]{3})*\.?[0-9]{0,2})',
        # "191,425.43 CR" at end of message (common FirstBank format)
        r'([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*CR\s*$',
    ]
    
    for pattern in strict_balance_patterns:
        match = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
        if match:
            balance_str = match.group(1).replace(",", "")
            try:
                balance = float(balance_str)
                if balance < 1_000_000_000:  # Max 1 billion NGN balance
                    result["balance"] = balance
                    break
            except:
                pass
    
    # --- STEP 6: Extract narration/description ---
    # Order matters: explicit "Narration/Description/Remarks" labels are the
    # ground truth in FirstBank/NIBSS alerts. The loose "transfer to/from"
    # fallback is LAST and deliberately strict, so it can't accidentally grab
    # the greeting line ("Hello Iyalla Samuel...") as a counterparty name.
    narration_patterns = [
        r'Narration[:\s]*(.+?)(?:\n|$|Cleared|Balance)',
        r'Description[:\s]*(.+?)(?:\n|$)',
        r'Remarks[:\s]*(.+?)(?:\n|$)',
        # Only treat "transfer to / payment to / received from" as a counterparty,
        # never a bare "from" (which appears in "transaction notification ... from").
        r'(?:transfer\s*to|payment\s*to|received\s*from)[:\s]*([^.\n]+)',
    ]
    
    for pattern in narration_patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            narration = match.group(1).strip()
            # Clean up narration - remove reference numbers
            narration = re.sub(r'\b[0-9]{10,}\b', '', narration)  # Remove long numbers
            result["narration"] = narration[:100]
            break
    
    # --- STEP 7: Final validation ---
    # If we still couldn't find an amount, mark for review (don't use AI)
    if result["amount"] is None:
        result["needs_review"] = True
    
    return result


def extract_with_gemini(email_body, sender_email=""):
    """
    AI-POWERED: Use Gemini to extract financial data from ANY bank email format worldwide.
    Handles Nigerian, African, European, American, Asian banks - all formats.
    """
    try:
        prompt = f"""You are a financial data extraction expert. Extract transaction data from this bank alert email.
This could be from ANY bank in the world (Nigeria, Mauritius, Kenya, South Africa, UK, USA, India, etc.).

EXTRACT and return ONLY a JSON object with these keys (use null if not found):
{{
    "amount": <number - the transaction amount WITHOUT currency symbol>,
    "transaction_type": "<'credit' for money IN, 'debit' for money OUT>",
    "balance": <number - available/cleared balance after transaction>,
    "narration": "<brief description - recipient name, purpose, merchant>",
    "currency": "<3-letter code: NGN, USD, EUR, GBP, KES, ZAR, MUR, INR, etc.>",
    "bank_name": "<name of the bank if identifiable>"
}}

RULES:
- Amount: Extract the main transaction amount (not fees/charges)
- For "DR" or "debit" = transaction_type is "debit"
- For "CR" or "credit" = transaction_type is "credit"  
- Balance: Look for "balance", "available balance", "cleared balance"
- Currency: Detect from symbols (₦=NGN, $=USD, £=GBP, €=EUR, ₹=INR, R=ZAR)

Email from: {sender_email}
Email content:
{email_body[:3000]}

Respond with ONLY the JSON object, no markdown, no explanation."""

        response = model.generate_content(prompt)
        response_text = response.text.strip()
        
        # Clean up response
        import json
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1])  # Remove first and last lines
            if response_text.startswith("json"):
                response_text = response_text[4:]
        
        data = json.loads(response_text)
        return data
    except Exception as e:
        print(f"Gemini extraction failed: {e}")
        return None


def extract_amount_from_body(body, subject):
    """Extract amount from email body for receipts/invoices - supports GLOBAL currencies"""
    import re
    
    # Combine subject and body for searching
    full_text = f"{subject} {body}"
    
    # Multiple currency patterns - GLOBAL SUPPORT
    patterns = [
        # USD patterns
        r'\$\s*([0-9,]+\.?\d*)',
        r'USD\s*([0-9,]+\.?\d*)',
        r'([0-9,]+\.?\d*)\s*(?:USD|dollars?)',
        # NGN (Nigeria) patterns
        r'NGN\s*([0-9,]+\.?\d*)',
        r'₦\s*([0-9,]+\.?\d*)',
        # EUR patterns
        r'€\s*([0-9,]+\.?\d*)',
        r'EUR\s*([0-9,]+\.?\d*)',
        # GBP (UK) patterns
        r'£\s*([0-9,]+\.?\d*)',
        r'GBP\s*([0-9,]+\.?\d*)',
        # ZAR (South Africa) patterns
        r'R\s*([0-9,]+\.?\d*)',
        r'ZAR\s*([0-9,]+\.?\d*)',
        # KES (Kenya) patterns
        r'KES\s*([0-9,]+\.?\d*)',
        r'Ksh\s*([0-9,]+\.?\d*)',
        # MUR (Mauritius) patterns
        r'MUR\s*([0-9,]+\.?\d*)',
        r'Rs\s*([0-9,]+\.?\d*)',
        # INR (India) patterns
        r'₹\s*([0-9,]+\.?\d*)',
        r'INR\s*([0-9,]+\.?\d*)',
        # GHS (Ghana) patterns
        r'GH[Ss₵]\s*([0-9,]+\.?\d*)',
        r'GHS\s*([0-9,]+\.?\d*)',
        # Generic total/amount patterns
        r'total[:\s]*[₦$€£₹]?\s*([0-9,]+\.?\d*)',
        r'amount[:\s]*\$?\s*([0-9,]+\.?\d*)',
        r'price[:\s]*\$?\s*([0-9,]+\.?\d*)',
        r'charged[:\s]*\$?\s*([0-9,]+\.?\d*)',
        # Google Play pattern
        r'([0-9,]+\.?\d*)\s*(?:GBP|EUR|USD|NGN)'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, full_text, re.IGNORECASE)
        if match:
            amount_str = match.group(1).replace(",", "")
            try:
                amount = float(amount_str)
                if amount > 0:
                    return amount
            except:
                pass
    
    return None


def parse_gmail_message(msg_data):
    """Parse Gmail message data into our email format (legacy - kept for compatibility)"""
    return parse_gmail_message_full(msg_data)


def categorize_email(sender, subject):
    """Categorize email type based on sender and subject"""
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    
    # Nigerian Bank Alerts (prioritize this)
    nigerian_banks = ["firstbank", "gtbank", "zenith", "wema", "alat", "access", "sterling", 
                      "fidelity", "polaris", "fcmb", "union", "stanbic", "ecobank"]
    if any(bank in sender_lower for bank in nigerian_banks):
        if "debit" in subject_lower:
            return "bank_debit"
        elif "credit" in subject_lower:
            return "bank_credit"
        return "bank_alert"
    
    # International Bank/Transaction alerts
    if any(word in sender_lower for word in ["bank", "chase", "wells", "citi", "capital one"]):
        return "bank"
    
    # Subscriptions
    if any(word in sender_lower for word in ["netflix", "spotify", "hulu", "disney", "apple", "amazon prime", 
                                              "youtube", "audible", "snapchat", "jira", "google play"]):
        return "subscription"
    
    # Bills
    if any(word in subject_lower for word in ["bill", "invoice", "statement", "due", "utility"]):
        return "bill"
    
    # Receipts
    if any(word in subject_lower for word in ["receipt", "order", "confirmation", "shipped", "delivered"]):
        return "receipt"
    
    # Payments
    if any(word in subject_lower for word in ["payment", "paid", "transaction"]):
        return "payment"
    
    return "receipt"


def extract_amount(text):
    """Extract dollar amount from text"""
    import re
    
    # Look for patterns like $XX.XX, $X,XXX.XX, etc.
    patterns = [
        r'\$[\d,]+\.?\d*',
        r'[\d,]+\.?\d*\s*(?:USD|dollars?)',
        r'total[:\s]*\$?[\d,]+\.?\d*'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Extract just the number
            amount_str = re.sub(r'[^\d.]', '', match.group())
            try:
                return float(amount_str)
            except:
                pass
    
    return None


def get_category(sender, subject):
    """Determine spending category"""
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    
    if any(word in sender_lower for word in ["uber", "lyft", "grab"]):
        return "Transport"
    if any(word in sender_lower for word in ["netflix", "spotify", "hulu", "disney", "youtube"]):
        return "Entertainment"
    if any(word in sender_lower for word in ["amazon", "ebay", "walmart", "target"]):
        return "Shopping"
    if any(word in subject_lower for word in ["food", "restaurant", "doordash", "grubhub"]):
        return "Food"
    if any(word in subject_lower for word in ["electric", "utility", "water", "gas", "internet"]):
        return "Utilities"
    if any(word in sender_lower for word in ["bank", "chase", "wells"]):
        return "Banking"
    
    return "Other"


def detect_currency(body, subject, sender_email):
    """Detect currency from email content - supports global currencies"""
    import re
    
    text = f"{body} {subject} {sender_email}".lower()
    
    # Check for currency symbols first
    if "₦" in body or "ngn" in text:
        return "NGN"
    if "₹" in body or "inr" in text:
        return "INR"
    if "€" in body or "eur" in text:
        return "EUR"
    if "£" in body or "gbp" in text:
        return "GBP"
    if "ksh" in text or "kes" in text:
        return "KES"
    if "zar" in text or ("r " in text and any(b in text for b in ["fnb", "absa", "nedbank", "capitec", "standardbank"])):
        return "ZAR"
    if "gh₵" in body or "ghs" in text or "cedis" in text:
        return "GHS"
    if "mur" in text or ("rs" in text and any(b in text for b in ["mcb", "sbm", "afrasia"])):
        return "MUR"
    
    # Check sender domain for Nigerian banks -> NGN
    nigerian_indicators = [
        "wemabank", "firstbank", "gtbank", "zenithbank", "accessbank",
        "sterlingbank", "fidelitybank", "polarisbank", "fcmb", "unionbank",
        "stanbicibtc", "ecobank.ng", "ubagroup", "opay", "palmpay",
        "flutterwave", "paystack", "interswitch"
    ]
    if any(ind in sender_email.lower() for ind in nigerian_indicators):
        return "NGN"
    
    # Default to USD for international services
    return "USD"


@app.post("/sync-gmail-data")
async def sync_gmail_data(data: dict):
    """Sync Gmail email data to database for AI context"""
    try:
        user_id = data.get("user_id", "default_user")
        incoming_emails = data.get("emails", [])
        stats = data.get("stats", {})
        
        # Store in ChromaDB for this user
        doc_id = f"gmail_{user_id}"
        
        # ============================================
        # MERGE (do NOT overwrite) - critical for delta sync
        # ============================================
        # The delta sync only sends emails that arrived since the last bookmark
        # (often 0). If we blindly replaced the stored blob with that small/empty
        # list, we'd wipe all previously stored emails and blank the dashboard.
        # Instead, load what's already stored and merge the new emails in,
        # deduping by email id.
        existing_emails = []
        try:
            prev = gmail_data_collection.get(ids=[doc_id])
            if prev["documents"]:
                prev_doc = json.loads(prev["documents"][0])
                existing_emails = prev_doc.get("emails", [])
        except Exception as e:
            print(f"[Sync] No existing Gmail blob to merge (starting fresh): {e}")

        merged_by_id = {}
        for e in existing_emails:
            if e.get("id"):
                merged_by_id[e["id"]] = e
        new_count = 0
        for e in incoming_emails:
            eid = e.get("id")
            if eid and eid not in merged_by_id:
                new_count += 1
            if eid:
                merged_by_id[eid] = e  # newer copy wins for the same id

        emails = list(merged_by_id.values())

        # Replace the single blob with the merged set.
        try:
            gmail_data_collection.delete(ids=[doc_id])
        except:
            pass
        
        gmail_data_collection.add(
            documents=[json.dumps({"emails": emails, "stats": stats})],
            metadatas=[{
                "user_id": user_id,
                "email_count": len(emails),
                "synced_at": datetime.now().isoformat()
            }],
            ids=[doc_id]
        )

        # Mirror the same blob into Supabase `gmail_data` so it stops being empty
        # and the data survives outside the local ChromaDB vault. Best-effort:
        # a Supabase hiccup must never break the (working) ChromaDB sync above.
        try:
            mirror = database.save_gmail_data(user_id, emails, stats)
            if mirror["status"] != "success":
                print(f"[sync-gmail] Supabase mirror failed: {mirror['error']}")
        except Exception as e:
            print(f"[sync-gmail] Supabase mirror exception: {e}")

        print(
            f"Gmail data synced for user {user_id}: {new_count} new, "
            f"{len(emails)} total stored ({len(incoming_emails)} received this sync)"
        )
        
        # Report the balance via the SHARED snapshot so the greeting the
        # frontend shows matches the dashboard and chat exactly (chronological
        # pick by internal_date_ms, not a fragile date-string sort).
        bank_alerts = [e for e in emails if e.get("is_bank_alert")]
        snap = compute_bank_snapshot(user_id)

        return {
            "status": "success",
            "synced": len(emails),
            "bank_alerts_found": len(bank_alerts),
            "latest_bank_balance": snap["balance"]
        }
        
    except Exception as e:
        print(f"Sync Gmail Error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.post("/clear-gmail-data")
async def clear_gmail_data(data: dict):
    """Delete a single user's stored Gmail blob.

    Called on logout so a browser can be handed to / logged into by another
    account without the previous user's Gmail-derived transactions leaking into
    the new account's dashboard and spending analysis.
    """
    try:
        user_id = data.get("user_id", "default")
        doc_id = f"gmail_{user_id}"
        try:
            gmail_data_collection.delete(ids=[doc_id])
        except Exception:
            pass
        return {"status": "success", "cleared": doc_id}
    except Exception as e:
        print(f"Clear Gmail Error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.post("/api/verify-balance")
async def verify_balance(data: dict):
    """
    Bank Alert Auditing - Compare bank's 'Truth Anchor' balance with internal tracking.
    This is the core "True-Up" feature for Loamy.
    """
    try:
        user_id = data.get("user_id", "default_user")
        
        # 1. Get latest bank balance from Gmail data
        bank_balance = None
        bank_name = None
        last_alert_date = None
        
        try:
            # Use the shared snapshot so the "bank truth" balance here matches
            # the dashboard and chat exactly (chronological pick, summed per bank).
            snap = compute_bank_snapshot(user_id)
            if snap.get("alert_count", 0) > 0:
                bank_balance = snap["balance"]
                bank_name = snap.get("bank_name")
                last_alert_date = snap.get("last_date")
        except Exception as e:
            print(f"Error getting Gmail data: {e}")
        
        # 2. Calculate internal balance from accounts (Supabase, user-scoped)
        internal_balance = 0
        try:
            acct_res = database.get_accounts(user_id)
            if acct_res["status"] == "success":
                for account in acct_res["data"]:
                    internal_balance += float(account.get("balance", 0) or 0)
        except Exception as e:
            print(f"Error getting accounts: {e}")
        
        # 3. Calculate discrepancy
        discrepancy = None
        discrepancy_message = None
        needs_review = False
        
        if bank_balance is not None:
            discrepancy = bank_balance - internal_balance
            
            if abs(discrepancy) > 100:  # More than ₦100 or $100 difference
                needs_review = True
                if discrepancy > 0:
                    discrepancy_message = f"Your {bank_name or 'bank'} shows ₦{bank_balance:,.2f}, but Loamy only tracked ₦{internal_balance:,.2f}. Did we miss ₦{abs(discrepancy):,.2f} in income?"
                else:
                    discrepancy_message = f"Your {bank_name or 'bank'} shows ₦{bank_balance:,.2f}, but Loamy tracked ₦{internal_balance:,.2f}. Did we miss ₦{abs(discrepancy):,.2f} in expenses?"
        
        # 4. Get unverified transactions (bank alerts not matched to manual entries)
        unverified_transactions = []
        try:
            for email in load_user_emails(user_id):
                if email.get("is_bank_alert") and email.get("amount"):
                    # For now, mark all bank alerts as needing potential review.
                    unverified_transactions.append({
                        "date": email.get("date"),
                        "amount": email.get("amount"),
                        "type": email.get("transaction_type"),
                        "narration": email.get("narration"),
                        "bank": email.get("bank_name"),
                        "is_verified": False  # TODO: Match against manual entries
                    })
        except Exception as e:
            print(f"Error loading unverified transactions: {e}")
        
        return {
            "status": "success",
            "bank_truth": {
                "balance": bank_balance,
                "bank_name": bank_name,
                "last_alert_date": last_alert_date
            },
            "internal_tracking": {
                "balance": internal_balance
            },
            "verification": {
                "discrepancy": discrepancy,
                "discrepancy_message": discrepancy_message,
                "needs_review": needs_review
            },
            "unverified_transactions": unverified_transactions[:10]  # Limit to 10
        }
        
    except Exception as e:
        print(f"Verify Balance Error: {str(e)}")
        return {"status": "error", "error": str(e)}


@app.get("/api/bank-transactions/{user_id}")
async def get_bank_transactions(user_id: str):
    """Get all bank transactions extracted from Gmail for a user"""
    try:
        # Supabase-primary loader (ChromaDB fallback) - same source as the
        # dashboard/chat so this endpoint can't drift out of sync.
        emails = load_user_emails(user_id)
        if not emails:
            return {"status": "success", "transactions": [], "summary": {}}

        # Filter bank alerts
        bank_transactions = []
        total_credits = 0
        total_debits = 0
        
        for email in emails:
            if email.get("is_bank_alert"):
                tx = {
                    "id": email.get("id"),
                    "date": email.get("date"),
                    "amount": email.get("amount"),
                    "type": email.get("transaction_type"),
                    "balance_after": email.get("balance"),
                    "narration": email.get("narration"),
                    "bank": email.get("bank_name"),
                    "category": categorize_transaction(email.get("narration", ""))
                }
                bank_transactions.append(tx)
                
                if email.get("amount"):
                    if email.get("transaction_type") == "credit":
                        total_credits += email.get("amount")
                    elif email.get("transaction_type") == "debit":
                        total_debits += email.get("amount")
        
        # Sort by date (newest first)
        bank_transactions.sort(key=lambda x: x.get('date', ''), reverse=True)
        
        return {
            "status": "success",
            "transactions": bank_transactions,
            "summary": {
                "total_transactions": len(bank_transactions),
                "total_credits": total_credits,
                "total_debits": total_debits,
                "net_flow": total_credits - total_debits
            }
        }
        
    except Exception as e:
        print(f"Get Bank Transactions Error: {str(e)}")
        return {"status": "error", "error": str(e)}


def normalize_category(category):
    """
    Map stored/legacy category names to their canonical display name.
    Single responsibility: name normalization only (no I/O, no math).
    Keeps the dashboard, breakdown and review queue consistent.
    """
    if not category:
        return "Other"
    aliases = {
        "home improvement": "Personal Expenses",
        "home_improvement": "Personal Expenses",
        "personal": "Personal Expenses",
    }
    return aliases.get(str(category).strip().lower(), category)


def categorize_transaction(narration):
    """Categorize a bank transaction based on its narration"""
    if not narration:
        return "Other"

    narration_lower = narration.lower()
    
    # Business categories
    if any(word in narration_lower for word in ["transfer to", "nip transfer", "payment"]):
        return "Transfer"
    if any(word in narration_lower for word in ["pos", "purchase", "buy"]):
        return "Purchase"
    if any(word in narration_lower for word in ["airtime", "mtn", "glo", "airtel", "9mobile"]):
        return "Airtime"
    if any(word in narration_lower for word in ["dstv", "gotv", "startimes", "electricity", "nepa"]):
        return "Bills"
    if any(word in narration_lower for word in ["salary", "wage", "income"]):
        return "Income"
    if any(word in narration_lower for word in ["atm", "withdrawal", "cash"]):
        return "Cash Withdrawal"
    if any(word in narration_lower for word in ["stamp duty", "charge", "fee", "vat"]):
        return "Bank Charges"
    
    return "Other"
