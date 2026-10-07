"""
routes.py

Every FastAPI endpoint for the app: chat webhook glue, accounts, goals/bills,
expenses, invoices, the review queue, Gmail/Google OAuth + sync endpoints,
onboarding, auth (signup/login/session), notifications, and the dashboard.

All business logic lives in business_logic.py / email_parser.py / chat_service.py;
this module is just the HTTP surface that wires requests to that logic, so
endpoint paths, methods, and request/response shapes are unchanged from the
original main.py.
"""
import os
import re
import json
import asyncio
import threading
import hashlib
import secrets
import requests
from datetime import datetime, timedelta

from fastapi import APIRouter, UploadFile, File, HTTPException, Form, BackgroundTasks

import database
import categorizer
import chat_service
from models import (
    model,
    collection,
    goals_collection,
    accounts_collection,
    expenses_collection,
    chats_collection,
    users_collection,
    gmail_data_collection,
    pending_conversions_collection,
    invoices_collection,
    review_queue_collection,
    vendor_memory_collection,
    notifications_collection,
    sync_state_collection,
    gmail_credentials_collection,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    ConnectBankRequest,
)
from email_parser import (
    SUPPORTED_BANKS,
    get_connected_bank_accounts,
    email_belongs_to_bank,
)
from business_logic import (
    get_exchange_rate,
    convert_to_local_currency,
    save_pending_conversion,
    find_matching_pending_conversion,
    true_up_conversion,
    create_invoice_reminder,
    process_receipt_image,
    load_user_emails,
    compute_bank_snapshot,
    format_snapshot_for_ai,
    load_ledger_transactions,
    LEDGER_SOURCE_LABELS,
    normalize_category,
    hash_password,
    verify_password,
    to_sentence_case,
)
from email_parser import (
    _normalize_vendor,
    _extract_vendor_name,
    _build_nudge_message,
    _review_scan_cooldown_elapsed,
    _mark_review_scan,
    _scan_bank_emails_for_review,
    _get_stored_refresh_token,
    _mint_access_token,
    _server_sync_cooldown_elapsed,
    _mark_server_sync,
    run_server_side_gmail_sync,
    spawn_background_sync,
    maybe_autosync_all_users,
    get_last_synced_timestamp,
    set_last_synced_timestamp,
    _is_real_user_id,
    _background_fetch_and_store,
    _finish_sync_job,
    _background_store_only,
    _start_sync_job,
    _fetch_gmail_emails_core,
    get_email_body,
    parse_gmail_message_full,
    _ai_categorize_bank_alerts,
    _sync_gmail_data_core,
    INVALID_USER_IDS,
)


def _kill_switch_active() -> bool:
    # Mirrors main.py's owner kill switch flag so /health can report it
    # without importing back from main (which would create a circular import).
    return os.getenv("LOAMY_KILL_SWITCH", "").strip().lower() in ("on", "true", "1", "yes")


router = APIRouter()

_ONBOARDING_BANK_CACHE = {}
_active_bg_syncs = set()
_active_bg_syncs_lock = threading.Lock()
_bg_sync_results = {}
_gmail_blob_lock = threading.Lock()


@router.post("/chat")
async def chat_endpoint(query: dict):
    return await chat_service.chat_with_history(query)


@router.get("/health")
async def health():
    return {"status": "success", "data": {"disabled": _kill_switch_active()}, "error": None}



# Root route so Render's internal health check succeeds.
# Root cause of the "Timed Out" deploy failure: the app booted fine (uvicorn was
# up on port 10000), but Render pings GET / to decide the deploy is healthy and
# there was no "/" route - every probe got "GET / HTTP/1.1 404 Not Found", so the
# health check never saw a 2xx and Render eventually timed out and marked the
# deploy failed. A lightweight 200 at "/" (GET and HEAD, since Render probes with
# both) fixes that without affecting any existing endpoint.
@router.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "success", "data": {"service": "loamy-backend", "ok": True}, "error": None}


# Safety net only: CORSMiddleware above already answers preflight OPTIONS
# requests itself at the ASGI level before any route is matched, so this
# should normally never be hit. It exists in case some other layer (a future
# middleware, a proxy quirk, etc.) ever lets an OPTIONS request fall through.
@router.options("/{full_path:path}")
async def options_handler(full_path: str):
    return {"status": "ok"}



@router.get("/health/vector-store")
async def vector_store_health():
    return {"status": "success", "data": await asyncio.to_thread(_vector_store_health), "error": None}



# ============================================
# NOTIFICATIONS (in-app reminders, e.g. the daily 5pm invoice nudge)
# ============================================
# Single responsibility per endpoint. The frontend polls /notifications, shows a
# banner, and POSTs back to dismiss. The 5pm reminder itself is created by the
# scheduler's create_invoice_reminder() job above.


@router.get("/notifications")
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



@router.post("/notifications/dismiss")
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



@router.post("/notifications/trigger-invoice-reminder")
async def trigger_invoice_reminder():
    """Manually fire today's invoice reminder (handy for testing the 5pm nudge)."""
    result = create_invoice_reminder()
    return {"status": "success", "data": result, "error": None}



@router.post("/upload-artifact")
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



@router.get("/get-goals")
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

@router.get("/get-exchange-rate")
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



@router.post("/convert-currency")
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



@router.get("/get-pending-conversions")
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

@router.get("/list-all-transactions")
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



@router.delete("/delete-transaction/{transaction_id}")
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



@router.post("/cleanup-bad-data")
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



@router.post("/cleanup-gmail-data")
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



@router.get("/ledger-log/{user_id}")
async def get_ledger_log(user_id: str, limit: int = 8):
    """Recent WhatsApp cash entries and scanned receipts (source IN
    manual_cash, receipt) for the dashboard's log widget."""
    if not _is_real_user_id(user_id):
        return {"status": "error", "data": None, "error": "invalid_user_id"}
    try:
        rows = await asyncio.to_thread(load_ledger_transactions, user_id)
        items = [r for r in rows if r.get("source") in ("manual_cash", "receipt")]
        items.sort(key=lambda r: r.get("internal_date_ms") or 0, reverse=True)
        return {
            "status": "success",
            "data": {"items": items[:max(1, min(limit, 50))], "base_currency": "NGN"},
            "error": None,
        }
    except Exception as e:
        return {"status": "error", "data": None, "error": str(e)}



# ========== DASHBOARD ENDPOINT ==========

@router.get("/get-dashboard-data")
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
                                'suggested_categories': [c for c in categorizer.STANDARD_CATEGORIES if c != categorizer.UNCATEGORIZED]
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
            # Gemini categorizes at ingest time; anything still on a placeholder
            # is unresolved and belongs in "Uncategorized" (Review Queue).
            if category.strip().lower() in ('banking', 'other', 'uncategorized', ''):
                category = categorizer.UNCATEGORIZED
                tx['category'] = category
            
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
        
        # 3. Manual cash entries + scanned receipts, from the unified Supabase
        #    ledger (stored in NGN already, so no conversion here).
        for tx in load_ledger_transactions(user_id):
            if tx['type'] == 'credit':
                cash_in += tx['amount']
            else:
                cash_out += tx['amount']
                expense_categories[tx['category']] = expense_categories.get(tx['category'], 0) + tx['amount']
            recent_transactions.append(tx)
        
        # Sort recent transactions by precise time first (internal_date_ms),
        # falling back to the date string, so same-day items keep true order.
        recent_transactions.sort(key=lambda x: (x.get('internal_date_ms', 0) or 0, x.get('date', '')), reverse=True)
        recent_transactions = recent_transactions[:10]  # Keep only 10 most recent

        # Build a dedicated list of recent BANK transactions (credits + debits)
        # for the dashboard "Bank Accounts" section.
        bank_transactions.sort(key=lambda x: (x.get('internal_date_ms', 0) or 0, x.get('date', '')), reverse=True)
        recent_bank_transactions = bank_transactions[:10]  # Top 10 most recent bank movements
        
        # 5. Format expense breakdown for pie chart.
        # Uncategorized spend still counts toward cash_out/total_expenses (it's
        # real money leaving the account), but it's excluded from this
        # category breakdown since it hasn't been assigned a real category
        # yet. It shows up in the Review Queue instead.
        expense_breakdown = []
        colors = ['#2E7D32', '#D4A373', '#F4A261', '#1976D2', '#E63946', '#9C27B0', '#00BCD4', '#FF9800']
        categorized_expenses = {
            category: amount
            for category, amount in expense_categories.items()
            if category.strip().lower() != categorizer.UNCATEGORIZED.lower()
        }
        total_expenses = sum(categorized_expenses.values()) or 1
        
        for i, (category, amount) in enumerate(sorted(categorized_expenses.items(), key=lambda x: x[1], reverse=True)):
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

@router.post("/create-invoice")
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



@router.get("/get-invoices")
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



@router.post("/mark-invoice-paid/{invoice_id}")
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



@router.delete("/delete-invoice/{invoice_id}")
async def delete_invoice(invoice_id: str):
    """Delete an invoice."""
    try:
        invoices_collection.delete(ids=[invoice_id])
        return {"status": "success", "message": "Invoice deleted"}
    except Exception as e:
        return {"status": "error", "error": str(e)}



@router.post("/categorize-transaction")
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



@router.post("/review-queue/add")
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



@router.get("/review-queue/{user_id}")
async def get_review_queue(user_id: str):
    """Return all pending review items for a user (newest first).
    Also scans synced bank emails first so the queue stays fresh on its own."""
    if not _is_real_user_id(user_id):
        return {"status": "error", "data": None, "error": "invalid_user_id"}
    try:
        # Blocking ChromaDB work; keep it off the event loop so one slow scan
        # can't stall /get-chats and every other request on the instance.
        def _load():
            _scan_bank_emails_for_review(user_id)
            return review_queue_collection.get(where={"user_id": user_id})
        results = await asyncio.to_thread(_load)
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



@router.post("/review-queue/resolve")
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
        if source_id.startswith("ledger_"):
            res = database.update_transaction_category(source_id[len("ledger_"):], category)
            if res["status"] != "success":
                print(f"[REVIEW] Could not tag ledger transaction: {res['error']}")
        elif source_id:
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



@router.post("/review-queue/dismiss")
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



@router.post("/review-queue/seed-demo")
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



@router.post("/dedupe-goals")
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


@router.post("/add-bill")
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


@router.put("/update-goal")
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


@router.delete("/delete-goal")
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


@router.put("/rename-goal")
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


@router.post("/assign-to-goal")
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


@router.get("/get-accounts")
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


@router.post("/add-account")
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


@router.put("/update-account/{account_id}")
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


@router.delete("/delete-account/{account_id}")
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


@router.post("/allocate-funds")
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


@router.get("/get-expenses")
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


@router.post("/add-expense")
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


@router.put("/update-expense")
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


@router.delete("/delete-expense")
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



# ============================================
# CHAT MANAGEMENT ENDPOINTS
# ============================================


@router.get("/get-chats")
async def get_chats(user_id: str = "default"):
    """Sidebar chat list plus `history`: the user's unified, chronological
    chat_history across WhatsApp and the web app."""
    try:
        history_res = await asyncio.to_thread(database.get_chat_history, user_id, 200)
        history = (history_res.get("data") or []) if history_res.get("status") == "success" else []

        res = database.get_chats(user_id)
        if res["status"] != "success":
            return {"chats": [], "history": history, "error": res["error"]}

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
        return {"chats": chats, "history": history}

    except Exception as e:
        print(f"Get chats error: {str(e)}")
        return {"chats": [], "history": [], "error": str(e)}



@router.post("/create-chat")
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



@router.get("/get-chat/{chat_id}")
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



@router.put("/update-chat/{chat_id}")
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



@router.delete("/delete-chat/{chat_id}")
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



@router.put("/rename-chat/{chat_id}")
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



@router.get("/api/onboarding/banks")
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



@router.post("/api/onboarding/submit")
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



@router.post("/link-whatsapp")
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



@router.get("/whatsapp-link-status")
async def whatsapp_link_status(user_id: str):
    """Return whether this account has a WhatsApp number linked (for the UI)."""
    try:
        res = database.get_user_by_id(user_id)
        row = res.get("data") if res.get("status") == "success" else None
        phone = (row or {}).get("phone_number") or ""
        return {"status": "success", "linked": bool(phone), "phone_number": phone}
    except Exception as e:
        return {"status": "error", "linked": False, "error": str(e)}



@router.post("/signup")
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



@router.post("/login")
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



@router.get("/check-session/{user_id}")
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



@router.post("/google-auth")
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



@router.post("/gmail/exchange-token")
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



@router.post("/gmail/verify-token")
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



@router.post("/gmail/refresh-token")
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



@router.post("/gmail/store-credentials")
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



@router.post("/gmail/server-sync")
async def trigger_server_sync(data: dict):
    """Manual endpoint: force a server-side sync for the given user."""
    user_id = data.get("user_id", "default")
    result = await run_server_side_gmail_sync(user_id, force=True)
    return {"status": "success", "data": result, "error": None}



@router.get("/gmail/status/{user_id}")
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



@router.post("/gmail/fetch-emails")
async def fetch_gmail_emails(data: dict, background_tasks: BackgroundTasks):
    """Validate the token, then fetch + store emails in the background."""
    data = data or {}
    access_token = data.get("access_token")
    user_id = str(data.get("user_id") or "").strip()
    full_resync = bool(data.get("full_resync", False))

    if not access_token:
        return {"status": "error", "error": "No access token provided"}
    if not _is_real_user_id(user_id):
        return {"status": "error", "error": "invalid_user_id"}

    # Quick token check off the event loop so the frontend can still run its
    # refresh-token flow synchronously on a 401.
    try:
        probe = await asyncio.to_thread(
            requests.get,
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if probe.status_code == 401:
            return {
                "status": "error",
                "error": "token_expired",
                "message": "Your Gmail session has expired. Please reconnect your Gmail account.",
                "requires_reauth": True,
            }
    except Exception as e:
        return {"status": "error", "error": f"gmail_unreachable: {e}"}

    if not await _start_sync_job(user_id):
        return {"status": "processing", "already_running": True}

    background_tasks.add_task(_background_fetch_and_store, access_token, user_id, full_resync)
    return {"status": "processing"}



@router.get("/gmail/sync-status/{user_id}")
async def gmail_sync_status(user_id: str):
    """Poll target for the frontend after /gmail/fetch-emails returns 'processing'."""
    if not _is_real_user_id(user_id):
        return {"status": "error", "error": "invalid_user_id"}

    job_res = await asyncio.to_thread(database.get_sync_job, user_id)
    if job_res["status"] == "success":
        job = job_res["data"] or {}
        if job.get("status") == "processing":
            return {"status": "processing"}
        last = {
            "ok": job.get("status") != "failed",
            "reason": job.get("error_message"),
            "finished_at": job.get("updated_at"),
        }
    else:
        print(f"[BG Gmail] sync_jobs unavailable, using memory: {job_res['error']}")
        with _active_bg_syncs_lock:
            if user_id in _active_bg_syncs:
                return {"status": "processing"}
        last = _bg_sync_results.get(user_id) or {}

    def _read_blob():
        stored = gmail_data_collection.get(ids=[f"gmail_{user_id}"])
        if not stored["documents"]:
            return {"emails": [], "stats": {}}
        return json.loads(stored["documents"][0])

    try:
        blob = await asyncio.to_thread(_read_blob)
    except Exception as e:
        return {"status": "error", "error": str(e)}

    emails = sorted(
        blob.get("emails", []),
        key=lambda e: e.get("internal_date_ms") or 0,
        reverse=True,
    )[:500]
    return {
        "status": "success" if last.get("ok", True) else "error",
        "error": last.get("reason"),
        "emails": emails,
        "stats": blob.get("stats", {}),
        "finished_at": last.get("finished_at"),
    }



@router.post("/sync-gmail-data")
async def sync_gmail_data(data: dict, background_tasks: BackgroundTasks):
    """Validate the payload, then merge/store it in the background."""
    data = data or {}
    user_id = str(data.get("user_id") or "").strip()
    emails = data.get("emails", [])
    stats = data.get("stats", {})
    if not _is_real_user_id(user_id):
        return {"status": "error", "error": "invalid_user_id"}
    if not isinstance(emails, list) or not isinstance(stats, dict):
        return {"status": "error", "error": "invalid_payload"}
    saved = await asyncio.to_thread(database.set_sync_job, user_id, "processing")
    if saved["status"] == "error":
        print(f"[BG Gmail] could not persist sync job for {user_id}: {saved['error']}")
    background_tasks.add_task(
        _background_store_only, {"user_id": user_id, "emails": emails, "stats": stats}
    )
    return {"status": "processing"}



@router.post("/clear-gmail-data")
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



@router.post("/api/verify-balance")
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



@router.get("/api/bank-transactions/{user_id}")
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
                    "category": normalize_category(email.get("category"))
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





# ==========================================
# BANK CONNECTION MANAGEMENT (v1)
# Connected banks are stored as `accounts` rows tagged
# metadata.kind = "connected_bank", so no schema migration is needed and the
# legacy /get-accounts endpoint keeps working unchanged.
# ==========================================

def _connected_account_view(row):
    meta = row.get("metadata") or {}
    slug = meta.get("bank_slug")
    bank = SUPPORTED_BANKS.get(slug, {})
    return {
        "id": row.get("id"),
        "bank_slug": slug,
        "bank_name": bank.get("name", row.get("account_name")),
        "logo": bank.get("logo"),
        "nickname": meta.get("nickname") or "",
        "account_tail": meta.get("account_tail") or "",
        "currency": row.get("currency") or "NGN",
        "connected_at": meta.get("connected_at") or row.get("created_at"),
        "status": meta.get("status", "active"),
    }


def _alert_sort_key(email):
    return (email.get("internal_date_ms") or 0, email.get("date") or "")


def _summarize_bank_emails(emails):
    credits = sum(float(e.get("amount") or 0) for e in emails if e.get("transaction_type") == "credit")
    debits = sum(float(e.get("amount") or 0) for e in emails if e.get("transaction_type") == "debit")
    latest_balance = None
    last_activity = None
    for email in sorted(emails, key=_alert_sort_key, reverse=True):
        last_activity = last_activity or email.get("date")
        if email.get("balance") is not None:
            latest_balance = email.get("balance")
            break
    return {
        "balance": latest_balance,
        "transaction_count": len(emails),
        "total_credits": round(credits, 2),
        "total_debits": round(debits, 2),
        "net_flow": round(credits - debits, 2),
        "last_activity": last_activity,
    }


@router.get("/api/v1/banks/available")
async def list_available_banks():
    banks = [
        {"slug": slug, "name": bank["name"], "logo": bank["logo"], "senders": bank["senders"]}
        for slug, bank in SUPPORTED_BANKS.items()
    ]
    return {"status": "success", "data": {"banks": banks}, "error": None}


@router.post("/api/v1/accounts/connect")
async def connect_bank_account(payload: ConnectBankRequest):
    if not _is_real_user_id(payload.user_id):
        raise HTTPException(status_code=400, detail="A valid user_id is required")
    slug = payload.bank_slug.strip().lower()
    bank = SUPPORTED_BANKS.get(slug)
    if not bank:
        raise HTTPException(status_code=400, detail=f"Unsupported bank '{payload.bank_slug}'")

    # Deterministic id: reconnecting the same bank/account upserts instead of duplicating.
    digest = hashlib.sha256(f"{payload.user_id}:{slug}:{payload.account_tail}".encode()).hexdigest()[:12]
    account_id = f"bank_{slug}_{digest}"
    nickname = payload.nickname.strip() or bank["name"]

    saved = database.add_account(
        user_id=payload.user_id, account_id=account_id,
        account_name=nickname, currency="NGN",
        document=f"Connected bank: {bank['name']}" + (f" ****{payload.account_tail}" if payload.account_tail else ""),
        kind="connected_bank", bank_slug=slug, account_tail=payload.account_tail,
        nickname=nickname, type="Bank", status="active",
        connected_at=datetime.utcnow().isoformat() + "Z",
    )
    if saved["status"] != "success":
        raise HTTPException(status_code=500, detail=saved["error"])

    # Pull this bank's alerts now rather than waiting for the next scheduled sync.
    try:
        spawn_background_sync(payload.user_id, force=True)
    except Exception as e:
        print(f"[Bank Connect] Background sync not started for {payload.user_id}: {e}")

    row = (saved["data"] or [{}])[0] if isinstance(saved["data"], list) else {}
    account = _connected_account_view(row) if row else {
        "id": account_id, "bank_slug": slug, "bank_name": bank["name"], "logo": bank["logo"],
        "nickname": nickname, "account_tail": payload.account_tail, "currency": "NGN", "status": "active",
    }
    return {"status": "success", "data": {"account": account}, "error": None}


@router.get("/api/v1/accounts")
async def list_connected_accounts(user_id: str = "default"):
    if not _is_real_user_id(user_id):
        raise HTTPException(status_code=400, detail="A valid user_id is required")
    rows = get_connected_bank_accounts(user_id)
    emails = load_user_emails(user_id) if rows else []

    accounts = []
    for row in rows:
        view = _connected_account_view(row)
        matched = [e for e in emails if email_belongs_to_bank(e, view["bank_slug"], view["account_tail"])]
        view["summary"] = _summarize_bank_emails(matched)
        accounts.append(view)

    total_balance = sum(float(a["summary"]["balance"] or 0) for a in accounts)
    return {
        "status": "success",
        "data": {"accounts": accounts, "total_balance": round(total_balance, 2), "count": len(accounts)},
        "error": None,
    }


@router.get("/api/v1/accounts/{account_id}/transactions")
async def get_connected_account_transactions(
    account_id: str,
    user_id: str = "default",
    type: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 100,
):
    if not _is_real_user_id(user_id):
        raise HTTPException(status_code=400, detail="A valid user_id is required")
    row = next((r for r in get_connected_bank_accounts(user_id) if r.get("id") == account_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Connected account not found")

    view = _connected_account_view(row)
    tx_type = type.strip().lower()
    if tx_type and tx_type not in ("debit", "credit"):
        raise HTTPException(status_code=400, detail="type must be 'debit' or 'credit'")
    limit = max(1, min(limit, 500))

    matched = [e for e in load_user_emails(user_id) if email_belongs_to_bank(e, view["bank_slug"], view["account_tail"])]
    filtered = [
        e for e in matched
        if (not tx_type or e.get("transaction_type") == tx_type)
        and (not start_date or (e.get("date") or "") >= start_date)
        and (not end_date or (e.get("date") or "") <= end_date)
    ]
    filtered.sort(key=_alert_sort_key, reverse=True)

    merchants = {}
    categories = {}
    monthly = {}
    for email in filtered:
        amount = float(email.get("amount") or 0)
        if email.get("transaction_type") == "debit":
            category = normalize_category(email.get("category"))
            cat_stats = categories.setdefault(category, {"category": category, "total": 0.0, "count": 0})
            cat_stats["total"] += amount
            cat_stats["count"] += 1
        month = (email.get("date") or "")[:7] or "unknown"
        bucket = monthly.setdefault(month, {"month": month, "credits": 0.0, "debits": 0.0})
        if email.get("transaction_type") == "credit":
            bucket["credits"] += amount
        elif email.get("transaction_type") == "debit":
            bucket["debits"] += amount
            merchant = email.get("narration") or "Unknown"
            stats = merchants.setdefault(merchant, {"merchant": merchant, "total": 0.0, "count": 0})
            stats["total"] += amount
            stats["count"] += 1

    transactions = [
        {
            "id": e.get("id"),
            "date": e.get("date"),
            "amount": e.get("amount"),
            "type": e.get("transaction_type"),
            "merchant": e.get("narration"),
            "balance_after": e.get("balance"),
            "account_tail": e.get("account_tail") or view["account_tail"],
            "currency": e.get("currency") or "NGN",
            "category": normalize_category(e.get("category")),
        }
        for e in filtered[:limit]
    ]

    analytics = _summarize_bank_emails(filtered)
    analytics["top_merchants"] = [
        {**m, "total": round(m["total"], 2)}
        for m in sorted(merchants.values(), key=lambda m: m["total"], reverse=True)[:5]
    ]
    analytics["top_categories"] = [
        {**c, "total": round(c["total"], 2)}
        for c in sorted(categories.values(), key=lambda c: c["total"], reverse=True)[:5]
    ]
    analytics["monthly"] = [
        {**b, "credits": round(b["credits"], 2), "debits": round(b["debits"], 2)}
        for b in sorted(monthly.values(), key=lambda b: b["month"])
    ]

    return {
        "status": "success",
        "data": {"account": view, "transactions": transactions, "analytics": analytics},
        "error": None,
    }
