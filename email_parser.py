"""
email_parser.py

Gmail OAuth/connection endpoint support, the delta-sync bookmark system,
server-side background sync jobs, and all bank-alert / vendor-receipt email
classification + parsing logic (the "Smart Mailroom Clerk").
"""
import re
import json
import time
import base64
import threading
import asyncio
import requests
from datetime import datetime, timedelta

import database
import categorizer
from business_logic import compute_bank_snapshot, load_ledger_transactions
from models import (
    model,
    gmail_data_collection,
    sync_state_collection,
    review_queue_collection,
    vendor_memory_collection,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
)

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

    # WhatsApp cash entries and receipts that Gemini couldn't place also belong
    # in the queue, so every "Uncategorized" item has one place to be fixed.
    for tx in load_ledger_transactions(user_id):
        if tx.get("source") not in ("manual_cash", "receipt"):
            continue
        if tx.get("category") != categorizer.UNCATEGORIZED:
            continue
        source_id = f"ledger_{tx['id']}"
        vendor_name = tx.get("description") or "Expense"
        try:
            already = review_queue_collection.get(where={"source_id": source_id})
            if already["ids"]:
                continue
            review_queue_collection.add(
                ids=[f"rev_{source_id}"],
                documents=[f"Review: {vendor_name} {tx['amount']}"],
                metadatas=[{
                    "user_id": user_id,
                    "vendor": vendor_name,
                    "amount": tx["amount"],
                    "transaction_type": tx.get("type") or "debit",
                    "date": tx.get("date") or "",
                    "source_id": source_id,
                    "status": "pending",
                    "nudge": _build_nudge_message(user_name, vendor_name, tx["amount"], tx.get("type") or "debit"),
                    "user_reply": "",
                    "category": "",
                    "created_at": tx.get("date") or "",
                }]
            )
            added += 1
        except Exception as e:
            print(f"[REVIEW] ledger scan add error: {e}")

    print(f"[REVIEW] scan checked {len(emails)} emails, queued {added} new item(s)")
    return added



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
        fetched = await _fetch_gmail_emails_core(
            {"access_token": access_token, "user_id": user_id, "full_resync": force}
        )
        if fetched.get("status") != "success":
            return {"synced": False, "reason": fetched.get("error", "fetch_failed")}
        await _sync_gmail_data_core(
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



INVALID_USER_IDS = {"", "default", "default_user", "null", "undefined", "none"}
_bg_sync_results = {}
# Serializes read-merge-write of the per-user Gmail blob across worker threads.
_gmail_blob_lock = threading.Lock()


def _is_real_user_id(user_id) -> bool:
    return isinstance(user_id, str) and user_id.strip().lower() not in INVALID_USER_IDS



def _background_fetch_and_store(access_token: str, user_id: str, full_resync: bool):
    """Runs in FastAPI's threadpool (sync def) with its own event loop, so the
    blocking Gmail/ChromaDB/Supabase work never touches the request event loop."""
    result = {"ok": False, "reason": None, "count": 0}
    try:
        fetched = asyncio.run(_fetch_gmail_emails_core(
            {"access_token": access_token, "user_id": user_id, "full_resync": full_resync}
        ))
        if fetched.get("status") != "success":
            result["reason"] = fetched.get("error", "fetch_failed")
            return
        with _gmail_blob_lock:
            asyncio.run(_sync_gmail_data_core({
                "user_id": user_id,
                "emails": fetched.get("emails", []),
                "stats": fetched.get("stats", {}),
            }))
        result.update(ok=True, count=len(fetched.get("emails", [])))
    except Exception as e:
        print(f"[BG Gmail] fetch+store failed for {user_id}: {e}")
        result["reason"] = str(e)
    finally:
        result["finished_at"] = datetime.now().isoformat()
        _bg_sync_results[user_id] = result
        _finish_sync_job(user_id, result["ok"], result.get("reason"))
        with _active_bg_syncs_lock:
            _active_bg_syncs.discard(user_id)



def _finish_sync_job(user_id: str, ok: bool, reason=None):
    saved = database.set_sync_job(
        user_id, "completed" if ok else "failed", None if ok else str(reason or "sync_failed")
    )
    if saved["status"] == "error":
        print(f"[BG Gmail] could not persist sync job for {user_id}: {saved['error']}")



def _background_store_only(payload: dict):
    user_id = payload.get("user_id")
    ok, reason = False, None
    try:
        with _gmail_blob_lock:
            asyncio.run(_sync_gmail_data_core(payload))
        ok = True
    except Exception as e:
        print(f"[BG Gmail] store failed for {user_id}: {e}")
        reason = str(e)
    finally:
        _bg_sync_results[user_id] = {
            "ok": ok, "reason": reason, "finished_at": datetime.now().isoformat()
        }
        _finish_sync_job(user_id, ok, reason)



async def _start_sync_job(user_id: str) -> bool:
    """True if this request owns a new job; False if one is already running.
    Supabase is the source of truth so a Render restart can't orphan the
    in-flight flag; the in-memory set is only a fallback if Supabase is down."""
    started = await asyncio.to_thread(database.try_start_sync_job, user_id)
    if started["status"] == "success":
        if started["data"]:
            with _active_bg_syncs_lock:
                _active_bg_syncs.add(user_id)
        return bool(started["data"])
    print(f"[BG Gmail] sync_jobs unavailable, using memory: {started['error']}")
    with _active_bg_syncs_lock:
        if user_id in _active_bg_syncs:
            return False
        _active_bg_syncs.add(user_id)
    return True



async def _fetch_gmail_emails_core(data: dict):
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
        keyword_sweep = f"{after_clause} (debit OR credit OR debited OR credited OR transaction OR transfer OR alert OR payment OR receipt OR invoice OR account)"

        # Users who connected specific banks get one sender-scoped query and
        # only those banks' alerts are parsed. Users who haven't connected any
        # bank keep the original broad sweep below.
        connected_slugs = get_connected_bank_slugs(user_id)
        allowed_bank_slugs = set(connected_slugs) if connected_slugs else None
        sender_filter = build_gmail_sender_query(connected_slugs)
        if sender_filter:
            print(f"INFO: [Bank Registry] {user_id} connected banks: {connected_slugs} -> {sender_filter}")

        search_queries = [keyword_sweep, f"{after_clause} {sender_filter}"] if sender_filter else [
            # Targeted financial keyword sweep, delta-filtered.
            keyword_sweep,

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

                    parsed_email = parse_gmail_message_full(msg_data, allowed_bank_slugs)
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



def parse_gmail_message_full(msg_data, allowed_bank_slugs=None):
    """Parse Gmail message with FULL body content for amount extraction.

    When `allowed_bank_slugs` is given, bank alerts from banks outside that
    set are skipped so only the user's connected banks are parsed."""
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
            if allowed_bank_slugs is not None and resolve_bank_slug(sender_email) not in allowed_bank_slugs:
                return None

            bank_data = extract_bank_alert_data(body, subject, sender_email)
            bank_slug, registry_data = parse_with_registry(body, sender_email)
            account_tail = ""
            if registry_data:
                if registry_data.get("amount") is not None:
                    bank_data["amount"] = registry_data["amount"]
                    bank_data["needs_review"] = False
                if registry_data.get("type"):
                    bank_data["transaction_type"] = registry_data["type"]
                if registry_data.get("balance") is not None:
                    bank_data["balance"] = registry_data["balance"]
                if registry_data.get("merchant"):
                    bank_data["narration"] = registry_data["merchant"]
                account_tail = registry_data.get("account_tail") or ""
            if bank_slug and not detected_bank:
                detected_bank = SUPPORTED_BANKS[bank_slug]["name"]
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
                "bank_slug": bank_slug,
                "account_tail": account_tail,
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


# ============================================
# MULTI-BANK PARSER REGISTRY
# ============================================
# SUPPORTED_BANKS is the single source of truth for which banks a user can
# connect, which Gmail senders belong to each bank, and which parser handles
# their alerts. Each parser takes the plain-text email body and returns:
#   {"amount": float|None, "type": "debit"|"credit"|None, "account_tail": str,
#    "merchant": str, "date": str, "currency": "NGN", "balance": float|None}
# Fields a parser can't find stay None/"" so the caller can fall back to the
# generic extract_bank_alert_data() result instead of overwriting good data.

def _logo_for(domain):
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


SUPPORTED_BANKS = {
    "wema": {
        "name": "Wema Bank",
        "senders": ["wemaalert@wemabank.com", "wemabank.com", "alat.ng"],
        "logo": _logo_for("wemabank.com"),
        "aliases": ["wema", "alat"],
    },
    "firstbank": {
        "name": "First Bank",
        "senders": ["FirstAlert@firstbanknigeria.com", "firstbanknigeria.com"],
        "logo": _logo_for("firstbanknigeria.com"),
        "aliases": ["firstbank", "first bank", "firstalert"],
    },
    "gtbank": {
        "name": "GTBank",
        "senders": ["gtbank.com", "gtbplc.com"],
        "logo": _logo_for("gtbank.com"),
        "aliases": ["gtbank", "gtb", "guaranty"],
    },
    "zenith": {
        "name": "Zenith Bank",
        "senders": ["zenithbank.com"],
        "logo": _logo_for("zenithbank.com"),
        "aliases": ["zenith"],
    },
    "access": {
        "name": "Access Bank",
        "senders": ["accessbankplc.com"],
        "logo": _logo_for("accessbankplc.com"),
        "aliases": ["access bank", "accessbank"],
    },
    "opay": {
        "name": "OPay",
        "senders": ["opay.com", "opay-inc.com"],
        "logo": _logo_for("opayweb.com"),
        "aliases": ["opay"],
    },
}

_AMOUNT_RE = r'([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)'

_ALERT_DATE_FORMATS = (
    "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d %b %Y %H:%M", "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d",
)


def _to_float(value):
    if not value:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _extract_labeled_fields(body, labels):
    """Slice `body` into {field: value} using label regexes as delimiters.

    Bank alert bodies arrive either line-broken (text/plain) or collapsed onto
    one line (HTML stripped by get_email_body), so values are bounded by the
    NEXT label found in the text rather than by newlines alone."""
    hits = []
    for field, pattern in labels.items():
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            hits.append((match.start(), match.end(), field))
    hits.sort()
    fields = {}
    for i, (_, end, field) in enumerate(hits):
        stop = hits[i + 1][0] if i + 1 < len(hits) else len(body)
        value = body[end:stop].split("\n")[0] if field != "balance" else body[end:stop]
        fields[field] = value.strip(" \t\r\n:-|")
    return fields


def _account_tail(raw):
    if not raw:
        return ""
    digit_runs = re.findall(r'\d+', raw)
    return digit_runs[-1][-4:] if digit_runs else ""


def _normalize_alert_date(raw):
    if not raw:
        return ""
    cleaned = re.sub(r'\s+', ' ', raw).strip()[:25]
    for candidate in (cleaned, cleaned[:19], cleaned[:16], cleaned[:11], cleaned[:10]):
        for fmt in _ALERT_DATE_FORMATS:
            try:
                return datetime.strptime(candidate.strip(), fmt).isoformat()
            except ValueError:
                continue
    return cleaned


def _detect_tx_type(type_label, body):
    label = (type_label or "").lower()
    if "debit" in label or label.strip() == "dr":
        return "debit"
    if "credit" in label or label.strip() == "cr":
        return "credit"
    lowered = body.lower()
    debit_at = lowered.find("debit")
    credit_at = lowered.find("credit")
    if debit_at == -1 and credit_at == -1:
        return None
    if credit_at == -1 or (debit_at != -1 and debit_at < credit_at):
        return "debit"
    return "credit"


def _clean_merchant(raw):
    if not raw:
        return ""
    merchant = re.sub(r'^(?:\s*(?:POS|WEB)\s*[/\\\-:|]?\s*)+', '', raw, flags=re.IGNORECASE)
    merchant = re.sub(r'\b[0-9]{10,}\b', '', merchant)
    return re.sub(r'\s+', ' ', merchant).strip(" /-|")[:100]


_WEMA_LABELS = {
    "account_number": r'Account\s*Number\s*:',
    "account_name": r'Account\s*Name\s*:',
    "amount": r'Transaction\s*Amount\s*:',
    "tx_type": r'Transaction\s*Type\s*:',
    "description": r'Description\s*:',
    "location": r'Transaction\s*Location\s*:',
    "reference": r'(?:Transaction\s*Reference|Reference|Session\s*ID)\s*:',
    "datetime": r'Transaction\s*Date\s*(?:&amp;|&|and)\s*Time\s*:',
    "value_date": r'Value\s*Date\s*:',
    "balance": r'Current\s*Balance\s*as\s*at',
    "available": r'Available\s*Balance\s*:',
}


def parse_wema_alert(email_body: str) -> dict:
    fields = _extract_labeled_fields(email_body or "", _WEMA_LABELS)

    amount_value = fields.get("amount", "")
    amount_match = re.search(_AMOUNT_RE + r'\s*NGN', amount_value, re.IGNORECASE) \
        or re.search(_AMOUNT_RE, amount_value)

    # "Current Balance as at 14-05-2026 13:45:40 : 307,775.68 NGN" - the label
    # carries a timestamp full of colons, so take the figure tagged with NGN.
    balance_value = fields.get("balance", "")
    balance_match = re.search(r':\s*' + _AMOUNT_RE + r'\s*NGN', balance_value, re.IGNORECASE) \
        or re.search(_AMOUNT_RE + r'\s*NGN', balance_value, re.IGNORECASE)

    return {
        "amount": _to_float(amount_match.group(1)) if amount_match else None,
        "type": _detect_tx_type(fields.get("tx_type"), email_body or ""),
        "account_tail": _account_tail(fields.get("account_number")),
        "merchant": _clean_merchant(fields.get("description")),
        "date": _normalize_alert_date(fields.get("datetime")),
        "currency": "NGN",
        "balance": _to_float(balance_match.group(1)) if balance_match else None,
    }


_FIRSTBANK_LABELS = {
    "account_number": r'(?:Account\s*Number|Acct\s*No\.?|Account\s*No\.?)\s*:',
    "account_name": r'Account\s*Name\s*:',
    "amount": r'(?:Transaction\s*Amount|Amount|Amt)\s*:',
    "tx_type": r'(?:Transaction\s*Type|Txn\s*Type)\s*:',
    "description": r'(?:Narration|Description|Remarks)\s*:',
    "datetime": r'(?:Transaction\s*Date|Date\s*(?:&amp;|&|and)?\s*Time|Date)\s*:',
    "reference": r'(?:Reference|Ref\.?|Session\s*ID)\s*:',
    "balance": r'(?:Cleared\s*Balance|Available\s*Balance|Current\s*Balance|Balance)\s*:',
}


def parse_firstbank_alert(email_body: str) -> dict:
    fields = _extract_labeled_fields(email_body or "", _FIRSTBANK_LABELS)
    amount_match = re.search(_AMOUNT_RE, fields.get("amount", "").replace("NGN", " "))
    balance_match = re.search(_AMOUNT_RE, fields.get("balance", "").replace("NGN", " "))
    return {
        "amount": _to_float(amount_match.group(1)) if amount_match else None,
        "type": _detect_tx_type(fields.get("tx_type"), email_body or ""),
        "account_tail": _account_tail(fields.get("account_number")),
        "merchant": _clean_merchant(fields.get("description")),
        "date": _normalize_alert_date(fields.get("datetime")),
        "currency": "NGN",
        "balance": _to_float(balance_match.group(1)) if balance_match else None,
    }


def _generic_registry_parser(email_body: str) -> dict:
    """Standard-shape adapter over the deterministic generic extractor, used
    for banks without a dedicated template parser yet."""
    data = extract_bank_alert_data(email_body or "", "", "")
    tail_match = re.search(r'(?:Account|Acct)[^:\n]{0,20}:\s*([0-9*Xx]{4,})', email_body or "", re.IGNORECASE)
    return {
        "amount": data.get("amount"),
        "type": data.get("transaction_type"),
        "account_tail": _account_tail(tail_match.group(1)) if tail_match else "",
        "merchant": _clean_merchant(data.get("narration")),
        "date": "",
        "currency": "NGN",
        "balance": data.get("balance"),
    }


BANK_PARSERS = {
    "wema": parse_wema_alert,
    "firstbank": parse_firstbank_alert,
    "gtbank": _generic_registry_parser,
    "zenith": _generic_registry_parser,
    "access": _generic_registry_parser,
    "opay": _generic_registry_parser,
}


def resolve_bank_slug(sender_email):
    """Map a sender address to its SUPPORTED_BANKS slug, or None."""
    sender_lower = (sender_email or "").lower()
    if not sender_lower:
        return None
    for slug, bank in SUPPORTED_BANKS.items():
        for sender in bank["senders"]:
            sender = sender.lower()
            if sender_lower == sender or sender_lower.endswith("@" + sender) or sender_lower.endswith("." + sender):
                return slug
    return None


def parse_with_registry(email_body, sender_email):
    """Run the registered parser for this sender's bank. Returns (slug, data)
    or (None, None) when the sender isn't a registered bank."""
    slug = resolve_bank_slug(sender_email)
    parser = BANK_PARSERS.get(slug)
    if not parser:
        return None, None
    try:
        return slug, parser(email_body)
    except Exception as e:
        print(f"[Bank Registry] {slug} parser failed: {e}")
        return slug, None


def get_connected_bank_accounts(user_id):
    """Connected-bank rows for a user (accounts tagged kind=connected_bank)."""
    if not _is_real_user_id(user_id):
        return []
    res = database.get_accounts(user_id)
    if res["status"] != "success":
        print(f"[Bank Registry] Could not load accounts for {user_id}: {res['error']}")
        return []
    connected = []
    for row in res["data"]:
        meta = row.get("metadata") or {}
        if meta.get("kind") == "connected_bank" and meta.get("bank_slug") in SUPPORTED_BANKS \
                and meta.get("status", "active") == "active":
            connected.append(row)
    return connected


def get_connected_bank_slugs(user_id):
    slugs = []
    for row in get_connected_bank_accounts(user_id):
        slug = (row.get("metadata") or {}).get("bank_slug")
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def build_gmail_sender_query(bank_slugs):
    """e.g. 'from:(wemaalert@wemabank.com OR FirstAlert@firstbanknigeria.com)'."""
    senders = []
    for slug in bank_slugs:
        for sender in SUPPORTED_BANKS.get(slug, {}).get("senders", []):
            if sender not in senders:
                senders.append(sender)
    if not senders:
        return ""
    return f"from:({' OR '.join(senders)})"


def email_belongs_to_bank(email, bank_slug, account_tail=""):
    """Whether a stored email record is an alert for this bank (and account
    tail, when both sides know it). Older records predate bank_slug, so fall
    back to the sender address and the stored bank_name."""
    if not email.get("is_bank_alert"):
        return False
    slug = email.get("bank_slug") or resolve_bank_slug(email.get("sender_email"))
    if not slug:
        bank_name = (email.get("bank_name") or "").lower()
        aliases = SUPPORTED_BANKS.get(bank_slug, {}).get("aliases", [])
        slug = bank_slug if any(alias in bank_name for alias in aliases) else None
    if slug != bank_slug:
        return False
    email_tail = email.get("account_tail") or ""
    if account_tail and email_tail and email_tail != account_tail:
        return False
    return True



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



async def _sync_gmail_data_core(data: dict):
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

        try:
            await asyncio.to_thread(_ai_categorize_bank_alerts, emails)
        except Exception as e:
            print(f"[categorizer] skipped this sync: {e}")

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



_AI_CATEGORIZE_PER_SYNC = 200


def _ai_categorize_bank_alerts(emails: list) -> int:
    """Assign a Gemini category to every bank alert still on the placeholder.

    Runs inside the background sync pipeline. Capped per sync so a first-time
    backfill of thousands of alerts spreads across syncs instead of stalling
    one. Low-confidence / failed results stay "Uncategorized" and are marked
    as checked so they surface in the Review Queue instead of being retried."""
    # Earlier syncs let Gemini file generic P2P/POS alerts as "Transfers & Cash".
    # Re-run the guardrails on those AI-assigned labels so they drop back into
    # the Review Queue. User-chosen categories (reviewed=True) are untouched.
    demoted = 0
    for e in emails:
        if not e.get("is_bank_alert") or e.get("reviewed"):
            continue
        current = str(e.get("category") or "").strip()
        is_ai_label = e.get("category_source") == "gemini" or current.lower() in ("transfer", "transfers", "transfers & cash")
        if not is_ai_label or current.lower() in ("", "uncategorized"):
            continue
        guarded = categorizer.apply_guardrails({
            "vendor": _extract_vendor_name(e.get("narration") or "", e.get("transaction_type") or "debit"),
            "description": e.get("narration") or e.get("subject") or "",
        }, {"category": categorizer.coerce_category(current), "confidence": e.get("category_confidence") or 0.0})
        if guarded["category"] == categorizer.UNCATEGORIZED:
            e["category"] = categorizer.UNCATEGORIZED
            e["category_source"] = "gemini"
            demoted += 1
    if demoted:
        print(f"[categorizer] {demoted} ambiguous transfer(s) moved back to Uncategorized")

    targets = [
        e for e in emails
        if e.get("is_bank_alert")
        and not e.get("category_source")
        and str(e.get("category") or "").strip().lower() in ("", "banking", "other", "uncategorized")
    ][:_AI_CATEGORIZE_PER_SYNC]
    if not targets:
        return demoted

    items = [{
        "amount": e.get("amount"),
        "type": e.get("transaction_type") or "debit",
        "vendor": _extract_vendor_name(e.get("narration") or "", e.get("transaction_type") or "debit"),
        "description": e.get("narration") or e.get("subject") or "",
    } for e in targets]

    results = categorizer.categorize_batch(items)
    for email, res in zip(targets, results):
        email["category"] = res["category"]
        email["category_confidence"] = res["confidence"]
        email["category_source"] = "gemini"
    resolved = sum(1 for r in results if r["category"] != categorizer.UNCATEGORIZED)
    print(f"[categorizer] {resolved}/{len(targets)} bank alerts categorized by Gemini")
    return len(targets) + demoted

