"""
business_logic.py

Financial calculations (currency conversion, exchange rates, the bank-vs-
internal "true-up" flow), the shared bank/ledger snapshot helpers used by the
dashboard/chat/WhatsApp advisor, receipt-image parsing (Gemini vision), and
small shared utilities (category normalization, password hashing).
"""
import os
import re
import json
import hashlib
from datetime import datetime, timedelta

import database
import categorizer
from models import (
    model,
    pending_conversions_collection,
    notifications_collection,
    gmail_data_collection,
)

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
        vendor = transaction_data.get('vendor') or 'Unknown'
        transaction_data['vendor'] = vendor
        items = transaction_data.get('items') or ''
        doc_text = f"Spent ₦{converted_amount} at {vendor} on {transaction_data['date']}. Items: {items}"
        if is_foreign_currency:
            doc_text += f" (Original: {original_currency}{numeric_total})"

        # Snap the vision model's category onto the standard list before saving,
        # so the dashboard breakdown and WhatsApp card agree with what's stored.
        category = categorizer.coerce_category(transaction_data.get('category'))
        if category == categorizer.UNCATEGORIZED:
            category = categorizer.categorize_one(
                converted_amount, vendor, transaction_data.get('description') or str(items) or vendor
            )["category"]
        transaction_data['category'] = category

        tx_id = f"trans_{os.urandom(4).hex()}"
        save_res = database.add_transaction(
            user_id=user_id,
            tx_id=tx_id,
            amount=converted_amount,          # stored in NGN
            vendor=vendor,
            description=vendor,
            original_amount=numeric_total,
            original_currency=original_currency,
            currency="NGN",
            date=transaction_data['date'],
            category=category,
            transaction_type="expense",
            is_estimate=is_foreign_currency,  # True until a bank alert trues it up
            document=doc_text,
            source="receipt",
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

        # Manual cash entries and scanned receipts count toward spending too.
        # They don't move the bank balance, which stays the bank's own figure.
        ledger_rows = load_ledger_transactions(user_id)
        for row in ledger_rows:
            amount = row['amount']
            if row['type'] == 'credit':
                snap['total_credits'] += amount
            else:
                snap['total_debits'] += amount
                cat = row['category']
                snap['spending_by_category'][cat] = snap['spending_by_category'].get(cat, 0) + amount
            alerts.append({
                'date': row['date'], 'internal_ms': row['internal_date_ms'], 'bank': row['bank'],
                'amount': amount, 'type': row['type'], 'narration': row['description'],
            })
        snap['ledger_count'] = len(ledger_rows)

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
        snap['alert_count'] = len(alerts) - snap.get('ledger_count', 0)
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
    ) or "  - No transactions yet."
    bank_str = f" (latest alert from {snap['bank_name']} on {snap['last_date']})" if snap.get("bank_name") else ""
    return f"""- Current Bank Balance: ₦{snap.get('balance', 0):,.2f}{bank_str}
- Total Money In: ₦{snap.get('total_credits', 0):,.2f}
- Total Money Out (bank alerts + cash + receipts): ₦{snap.get('total_debits', 0):,.2f}
- Bank alerts on file: {snap.get('alert_count', 0)}
- Cash entries and receipts on file: {snap.get('ledger_count', 0)}

=== SPENDING BY CATEGORY (all sources) ===
{cats}

=== RECENT TRANSACTIONS, ALL SOURCES (most recent first) ===
{txns}"""



LEDGER_SOURCE_LABELS = {"manual_cash": "Cash", "receipt": "Receipt", "bank_alert": "Bank"}



def load_ledger_transactions(user_id: str) -> list:
    """Every Supabase ledger row for this user (cash, receipts, logged alerts),
    normalized to the dashboard's transaction shape."""
    res = database.get_transactions(user_id, limit=1000)
    if res["status"] != "success":
        print(f"[v0] load_ledger_transactions failed: {res['error']}")
        return []
    rows = []
    for r in res["data"] or []:
        amount = float(r.get("amount") or 0)
        if amount <= 0:
            continue
        source = database.transaction_source(r)
        date_str = str(r.get("occurred_on") or r.get("date") or "")
        date_obj = database._to_date_obj(date_str)
        internal_ms = int(datetime.combine(date_obj, datetime.min.time()).timestamp() * 1000) if date_obj else 0
        tx_type = "credit" if (r.get("transaction_type") or "").lower() in ("in", "income", "credit", "deposit") else "debit"
        rows.append({
            "id": r.get("id"),
            "description": (r.get("description") or r.get("vendor") or "Expense")[:50],
            "amount": amount,
            "original_amount": r.get("original_amount"),
            "original_currency": r.get("original_currency") or "NGN",
            "type": tx_type,
            "date": date_obj.isoformat() if date_obj else date_str,
            "internal_date_ms": internal_ms,
            "bank": LEDGER_SOURCE_LABELS.get(source, "Ledger"),
            "category": categorizer.coerce_category(r.get("category")),
            "source": source,
        })
    return rows



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



def normalize_category(category):
    """
    Map stored/legacy category names to their canonical display name.
    Single responsibility: name normalization only (no I/O, no math).
    Keeps the dashboard, breakdown and review queue consistent.
    """
    if not category:
        return categorizer.UNCATEGORIZED
    aliases = {
        "home improvement": "Personal Expenses",
        "home_improvement": "Personal Expenses",
        "personal": "Personal Expenses",
    }
    key = str(category).strip().lower()
    if key in aliases:
        return aliases[key]
    # Legacy/placeholder labels snap onto the standard list; user-chosen
    # custom categories (e.g. "Personal Expenses") are kept as-is.
    coerced = categorizer.coerce_category(category)
    if coerced != categorizer.UNCATEGORIZED or key in ("other", "banking", "uncategorized"):
        return coerced
    return category



