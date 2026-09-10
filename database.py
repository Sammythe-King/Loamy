"""
Supabase data-access layer for Loamy (Phase 1: auth + core dashboard).

SINGLE RESPONSIBILITY (Atomic Modularity rule): this module is the ONLY place
that talks to the database. Everything else (main.py, whatsapp.py, chat.py)
calls these functions and never touches Supabase directly. Swap Supabase for
another Postgres later and only this file changes.

Every function returns the standard Loamy contract {status, data, error} so
callers always know what to expect (Clean API rule). Money is always quantized
to numeric(14,2) via _to_money (Ledger rule: never trust floats).
"""

import os
import json
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# --- Connection ---------------------------------------------------------------
# Backend MUST use the SERVICE ROLE key, not the publishable/anon key, so it can
# read/write regardless of RLS. The tables have RLS on with no public policies,
# so the anon key sees nothing; the service role key bypasses RLS by design.
SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_SECRET_KEY")
    or os.getenv("SUPABASE_SERVICE_KEY")
)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError(
        "Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY. Add them to your .env."
    )

if SUPABASE_KEY.startswith("sb_publishable_") or SUPABASE_KEY.startswith("eyJ") is False and "anon" in SUPABASE_KEY.lower():
    print("[Database] WARNING: this looks like a PUBLISHABLE/anon key. RLS will "
          "block backend writes. Use SUPABASE_SERVICE_ROLE_KEY (the secret key).")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================
# Helpers
# ============================================
def _to_money(amount) -> float:
    """Quantize any incoming amount to exactly 2 decimal places (numeric(14,2))."""
    try:
        q = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        q = Decimal("0.00")
    return float(q)


def _parse_date(value):
    """Best-effort parse of a 'YYYY-MM-DD' string into an ISO date for ordering.
    Returns None when the value is missing or unparseable (we keep the raw
    string in the `date` column regardless, so nothing is ever lost)."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        return None


def _ok(data):
    return {"status": "success", "data": data, "error": None}


def _err(message):
    return {"status": "error", "data": None, "error": str(message)}


# ============================================
# USERS  (custom email/password auth kept in Postgres)
# ============================================
def create_user(user_id: str, email: str, password_hash: str, **fields) -> dict:
    """Insert a new user. Extra onboarding fields go to typed columns when known
    and anything unrecognised is preserved in metadata (nothing lost)."""
    known = {"full_name", "business_type", "country", "selected_bank",
             "sender_domains", "status", "phone_number"}
    row = {
        "user_id": user_id,
        "email": email.lower().strip(),
        "password_hash": password_hash,
    }
    extra_meta = {}
    for k, v in fields.items():
        if k in known:
            row[k] = v
        else:
            extra_meta[k] = v
    if extra_meta:
        row["metadata"] = extra_meta
    try:
        res = supabase.table("users").insert(row).execute()
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        print(f"[Database] create_user failed: {e}")
        return _err(e)


def get_user_by_email(email: str) -> dict:
    try:
        res = (supabase.table("users").select("*")
               .eq("email", email.lower().strip()).limit(1).execute())
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        return _err(e)


def get_user_by_id(user_id: str) -> dict:
    try:
        res = supabase.table("users").select("*").eq("user_id", user_id).limit(1).execute()
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        return _err(e)


def get_user_by_phone(phone_number: str) -> dict:
    # Compare on digits-only so '+230 5717 4065', '23057174065', etc. all match
    # the WhatsApp-delivered form.
    try:
        digits = normalize_phone(phone_number)
        res = (supabase.table("users").select("*")
               .eq("phone_number", digits).limit(1).execute())
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        return _err(e)


def update_user(user_id: str, **fields) -> dict:
    try:
        res = supabase.table("users").update(fields).eq("user_id", user_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


def normalize_phone(phone: str) -> str:
    """Reduce any phone string to digits only (drop +, spaces, dashes).

    WhatsApp/Meta delivers the sender as bare digits (e.g. '23057174065'), so we
    store and compare in that same shape to make linking robust regardless of how
    the user typed it in the web app.
    """
    if not phone:
        return ""
    return "".join(ch for ch in str(phone) if ch.isdigit())


def ensure_user(user_id: str, email: str = None, full_name: str = None,
                phone_number: str = None, auth_provider: str = None) -> dict:
    """Create the Supabase users row if it doesn't exist, else update the given
    fields. This is the FOUNDATION every other table depends on: gmail_data,
    accounts, and chats all have a foreign key to users, so their inserts fail
    with 23503 until the parent user row exists here. Auth paths call this so a
    user created via Google (or before this change) is mirrored into Supabase.

    Upserts on user_id so it is safe to call on every login (self-healing).
    """
    row = {"user_id": user_id}
    if email is not None:
        row["email"] = email.lower().strip()
    if full_name is not None:
        row["full_name"] = full_name
    if phone_number is not None:
        row["phone_number"] = normalize_phone(phone_number)
    if auth_provider is not None:
        # auth_provider isn't a typed column; keep it in the jsonb metadata bag.
        row["metadata"] = {"auth_provider": auth_provider}
    try:
        # On CREATE we must satisfy any NOT NULL columns (e.g. password_hash for
        # Google users). On UPDATE we must NOT touch password_hash or we'd wipe a
        # real password, so only seed it when the row doesn't exist yet.
        existing = supabase.table("users").select("user_id").eq("user_id", user_id).limit(1).execute()
        if not (existing.data):
            row.setdefault("password_hash", "")
            row.setdefault("email", f"{user_id}@placeholder.loamy")
        res = supabase.table("users").upsert(row, on_conflict="user_id").execute()
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        print(f"[Database] ensure_user failed: {e}")
        return _err(e)


# ============================================
# TRANSACTIONS  (the ledger)
# ============================================
def add_transaction(user_id: str, tx_id: str, amount, **fields) -> dict:
    """Insert/replace a transaction. Uses the original chroma id as PK so a
    re-run of the same source can never duplicate a row (Ledger rule)."""
    known = {"vendor", "original_amount", "original_currency", "currency",
             "date", "category", "transaction_type", "source_id",
             "is_estimate", "document"}
    row = {"id": tx_id, "user_id": user_id, "amount": _to_money(amount)}
    extra_meta = {}
    for k, v in fields.items():
        if k in known:
            row[k] = v
        else:
            extra_meta[k] = v
    if "original_amount" in row and row["original_amount"] is not None:
        row["original_amount"] = _to_money(row["original_amount"])
    row["occurred_on"] = _parse_date(row.get("date"))
    if extra_meta:
        row["metadata"] = extra_meta
    try:
        res = supabase.table("transactions").upsert(row, on_conflict="id").execute()
        return _ok(res.data)
    except Exception as e:
        print(f"[Database] add_transaction failed: {e}")
        return _err(e)


def get_transactions(user_id: str, limit: int = 500) -> dict:
    try:
        res = (supabase.table("transactions").select("*")
               .eq("user_id", user_id)
               .order("occurred_on", desc=True)
               .order("created_at", desc=True)
               .limit(limit).execute())
        return _ok(res.data or [])
    except Exception as e:
        return _err(e)


def delete_transaction(tx_id: str) -> dict:
    try:
        res = supabase.table("transactions").delete().eq("id", tx_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


# ============================================
# GOALS
# ============================================
def add_goal(user_id: str, goal_id: str, **fields) -> dict:
    known = {"item", "amount", "assigned", "deadline", "category", "document"}
    row = {"id": goal_id, "user_id": user_id}
    extra_meta = {}
    for k, v in fields.items():
        if k in known:
            row[k] = v
        else:
            extra_meta[k] = v
    if "amount" in row:
        row["amount"] = _to_money(row["amount"])
    if "assigned" in row:
        row["assigned"] = _to_money(row["assigned"])
    if extra_meta:
        row["metadata"] = extra_meta
    try:
        res = supabase.table("goals").upsert(row, on_conflict="id").execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


def get_goals(user_id: str) -> dict:
    try:
        res = (supabase.table("goals").select("*")
               .eq("user_id", user_id).order("created_at", desc=True).execute())
        return _ok(res.data or [])
    except Exception as e:
        return _err(e)


def update_goal(goal_id: str, **fields) -> dict:
    if "amount" in fields:
        fields["amount"] = _to_money(fields["amount"])
    if "assigned" in fields:
        fields["assigned"] = _to_money(fields["assigned"])
    try:
        res = supabase.table("goals").update(fields).eq("id", goal_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


def delete_goal(goal_id: str) -> dict:
    try:
        res = supabase.table("goals").delete().eq("id", goal_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


# ============================================
# EXPENSES
# ============================================
def add_expense(user_id: str, expense_id: str, amount, **fields) -> dict:
    known = {"category", "date", "description", "document"}
    row = {"id": expense_id, "user_id": user_id, "amount": _to_money(amount)}
    extra_meta = {}
    for k, v in fields.items():
        if k in known:
            row[k] = v
        else:
            extra_meta[k] = v
    if extra_meta:
        row["metadata"] = extra_meta
    try:
        res = supabase.table("expenses").upsert(row, on_conflict="id").execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


def get_expenses(user_id: str) -> dict:
    try:
        res = (supabase.table("expenses").select("*")
               .eq("user_id", user_id).order("created_at", desc=True).execute())
        return _ok(res.data or [])
    except Exception as e:
        return _err(e)


def delete_expense(expense_id: str) -> dict:
    try:
        res = supabase.table("expenses").delete().eq("id", expense_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


# ============================================
# ACCOUNTS
# ============================================
def add_account(user_id: str, account_id: str, **fields) -> dict:
    known = {"account_name", "balance", "currency", "document"}
    row = {"id": account_id, "user_id": user_id}
    extra_meta = {}
    for k, v in fields.items():
        if k in known:
            row[k] = v
        else:
            extra_meta[k] = v
    if "balance" in row:
        row["balance"] = _to_money(row["balance"])
    if extra_meta:
        row["metadata"] = extra_meta
    try:
        res = supabase.table("accounts").upsert(row, on_conflict="id").execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


def get_accounts(user_id: str) -> dict:
    try:
        res = (supabase.table("accounts").select("*")
               .eq("user_id", user_id).order("created_at", desc=True).execute())
        return _ok(res.data or [])
    except Exception as e:
        return _err(e)


def delete_account(account_id: str) -> dict:
    try:
        res = supabase.table("accounts").delete().eq("id", account_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


# ============================================
# CHATS  (one row per conversation THREAD: title + full messages array)
# ============================================
def add_chat(user_id: str, chat_id: str, title: str = "New Chat",
             messages: list = None) -> dict:
    """Create (or replace) a chat thread. `messages` is the full conversation
    array, stored as JSON so the sidebar and transcript stay in one row."""
    now = datetime.now().isoformat()
    row = {
        "id": chat_id,
        "user_id": user_id,
        "title": (title or "New Chat")[:120],
        "messages": messages or [],
        "updated_at": now,
    }
    try:
        res = supabase.table("chats").upsert(row, on_conflict="id").execute()
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        print(f"[Database] add_chat failed: {e}")
        return _err(e)


def get_chats(user_id: str, limit: int = 200) -> dict:
    """List a user's chat threads, newest-updated first (for the sidebar)."""
    try:
        res = (supabase.table("chats")
               .select("id, title, messages, created_at, updated_at")
               .eq("user_id", user_id).order("updated_at", desc=True)
               .limit(limit).execute())
        return _ok(res.data or [])
    except Exception as e:
        return _err(e)


def get_chat(chat_id: str) -> dict:
    """Return one full chat thread (including its messages array)."""
    try:
        res = supabase.table("chats").select("*").eq("id", chat_id).limit(1).execute()
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        return _err(e)


def update_chat(chat_id: str, messages: list = None, title: str = None) -> dict:
    """Update a thread's messages and/or title; always bumps updated_at."""
    fields = {"updated_at": datetime.now().isoformat()}
    if messages is not None:
        fields["messages"] = messages
    if title is not None:
        fields["title"] = title[:120]
    try:
        res = supabase.table("chats").update(fields).eq("id", chat_id).execute()
        return _ok(res.data)
    except Exception as e:
        print(f"[Database] update_chat failed: {e}")
        return _err(e)


def delete_chat(chat_id: str) -> dict:
    try:
        res = supabase.table("chats").delete().eq("id", chat_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)


# ============================================
# AI context: deterministic financial snapshot for one user
# ============================================
def get_financial_context(user_id: str, recent_limit: int = 15) -> dict:
    """Accurate, user-scoped snapshot for the AI advisor. Deterministic Python
    does the math (Ledger rule); the AI only interprets the numbers."""
    try:
        rows = (supabase.table("transactions")
                .select("amount, transaction_type, category, vendor, date, occurred_on")
                .eq("user_id", user_id).limit(1000).execute()).data or []

        def _is_income(r):
            t = (r.get("transaction_type") or "").lower()
            return t in ("in", "income", "credit", "deposit")

        total_in = sum(_to_money(r["amount"]) for r in rows if _is_income(r))
        total_out = sum(_to_money(r["amount"]) for r in rows if not _is_income(r))
        balance = _to_money(total_in - total_out)

        by_category: dict[str, float] = {}
        for r in rows:
            if not _is_income(r):
                cat = r.get("category") or "other"
                by_category[cat] = _to_money(by_category.get(cat, 0) + _to_money(r["amount"]))

        return _ok({
            "balance": balance,
            "total_in": _to_money(total_in),
            "total_out": _to_money(total_out),
            "transaction_count": len(rows),
            "spending_by_category": by_category,
        })
    except Exception as e:
        print(f"[Database] get_financial_context failed: {e}")
        return _err(e)


# ============================================
# GMAIL DATA (one blob row per user: the synced bank-alert email array)
# ============================================
def save_gmail_data(user_id: str, emails: list, stats: dict = None) -> dict:
    """Upsert a user's synced Gmail blob. Row id mirrors the old Chroma doc id."""
    row = {
        "id": f"gmail_{user_id}",
        "user_id": user_id,
        "emails": emails or [],
        "stats": stats or {},
        "email_count": len(emails or []),
        "synced_at": datetime.now().isoformat(),
    }
    try:
        res = supabase.table("gmail_data").upsert(row, on_conflict="id").execute()
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        print(f"[Database] save_gmail_data failed: {e}")
        return _err(e)


def get_gmail_data(user_id: str) -> dict:
    """Return the stored Gmail blob row for a user, or None.

    `emails` is normalized to a real list whether Postgres handed back jsonb
    (already parsed) or a JSON string (legacy text column).
    """
    try:
        res = (supabase.table("gmail_data").select("*")
               .eq("id", f"gmail_{user_id}").limit(1).execute())
        row = res.data[0] if res.data else None
        if row is not None:
            emails = row.get("emails")
            if isinstance(emails, str):
                try:
                    emails = json.loads(emails)
                except (ValueError, TypeError):
                    emails = []
            # Some writers wrap the array as {"emails": [...]}.
            if isinstance(emails, dict):
                emails = emails.get("emails", [])
            row["emails"] = emails if isinstance(emails, list) else []
        return _ok(row)
    except Exception as e:
        print(f"[Database] get_gmail_data failed: {e}")
        return _err(e)


def categorize_narration(narration: str) -> str:
    """Categorize a bank alert from its narration text.

    Mirrors main.py's categorize_transaction so Gmail-derived spending lands in
    the same buckets as the rest of the app.
    """
    if not narration:
        return "Other"
    n = narration.lower()
    if any(w in n for w in ["transfer to", "nip transfer", "payment"]):
        return "Transfer"
    if any(w in n for w in ["pos", "purchase", "buy"]):
        return "Purchase"
    if any(w in n for w in ["airtime", "mtn", "glo", "airtel", "9mobile"]):
        return "Airtime"
    if any(w in n for w in ["dstv", "gotv", "startimes", "electricity", "nepa"]):
        return "Bills"
    if any(w in n for w in ["salary", "wage", "income"]):
        return "Income"
    if any(w in n for w in ["atm", "withdrawal", "cash"]):
        return "Cash Withdrawal"
    if any(w in n for w in ["stamp duty", "charge", "fee", "vat"]):
        return "Bank Charges"
    return "Other"


def get_gmail_financial_context(user_id: str, limit: int = 30) -> dict:
    """Parse a user's synced bank-alert emails into a financial snapshot.

    Returns latest bank balance (the bank's own 'truth anchor'), total credits
    and debits, spending by category, and the last `limit` bank transactions.
    """
    empty = {
        "bank_balance": None, "bank_name": None, "last_alert_date": None,
        "total_credits": 0.0, "total_debits": 0.0,
        "spending_by_category": {}, "recent_transactions": [], "alert_count": 0,
    }
    try:
        res = get_gmail_data(user_id)
        if res["status"] != "success" or not res["data"]:
            return _ok(empty)

        emails = res["data"].get("emails") or []
        alerts = [e for e in emails if e.get("is_bank_alert")]
        if not alerts:
            return _ok(empty)

        # Newest first, so the "latest balance" and recent list are consistent.
        alerts.sort(key=lambda e: e.get("date") or "", reverse=True)

        # Latest balance = most recent alert that actually carries a balance.
        bank_balance = bank_name = last_alert_date = None
        for a in alerts:
            if a.get("balance") is not None:
                bank_balance = _to_money(a.get("balance"))
                bank_name = a.get("bank_name")
                last_alert_date = a.get("date")
                break

        def _is_credit(a):
            return (a.get("transaction_type") or "").lower() in ("credit", "in", "income", "deposit")

        total_credits = total_debits = 0.0
        by_category: dict[str, float] = {}
        for a in alerts:
            amt = _to_money(a.get("amount") or 0)
            if not amt:
                continue
            if _is_credit(a):
                total_credits = _to_money(total_credits + amt)
            else:
                total_debits = _to_money(total_debits + amt)
                cat = a.get("category") or categorize_narration(a.get("narration", ""))
                by_category[cat] = _to_money(by_category.get(cat, 0) + amt)

        recent = []
        for a in alerts[:limit]:
            credit = _is_credit(a)
            recent.append({
                "date": a.get("date"),
                "vendor": a.get("narration") or a.get("bank_name") or "Bank alert",
                "amount": _to_money(a.get("amount") or 0),
                "currency": "NGN",
                "category": a.get("category") or categorize_narration(a.get("narration", "")),
                "type": "income" if credit else "expense",
                "source": "bank_alert",
            })

        return _ok({
            "bank_balance": bank_balance,
            "bank_name": bank_name,
            "last_alert_date": last_alert_date,
            "total_credits": total_credits,
            "total_debits": total_debits,
            "spending_by_category": by_category,
            "recent_transactions": recent,
            "alert_count": len(alerts),
        })
    except Exception as e:
        print(f"[Database] get_gmail_financial_context failed: {e}")
        return _err(e)


def get_chat_financial_context(user_id: str, limit: int = 50) -> dict:
    """Unified, user-scoped context for the AI chat.

    Merges TWO sources into one snapshot:
      1. the `transactions` table (scanned receipts, manual entries), and
      2. parsed bank-alert emails from the `gmail_data` table.

    Returns a summary (balance, total_income, total_spending,
    spending_by_category) plus a combined `recent_transactions` ledger.

    Balance preference: the bank's own latest alert balance is the truth anchor
    when available, since it reflects money the app may not have logged yet.
    All reads are strictly scoped by user_id.
    """
    try:
        rows = (supabase.table("transactions")
                .select("amount, transaction_type, category, vendor, date, occurred_on, currency")
                .eq("user_id", user_id)
                .order("occurred_on", desc=True)
                .order("created_at", desc=True)
                .limit(1000).execute()).data or []

        def _is_income(r):
            t = (r.get("transaction_type") or "").lower()
            return t in ("in", "income", "credit", "deposit")

        total_in = sum(_to_money(r["amount"]) for r in rows if _is_income(r))
        total_out = sum(_to_money(r["amount"]) for r in rows if not _is_income(r))
        balance = _to_money(total_in - total_out)

        by_category: dict[str, float] = {}
        for r in rows:
            if not _is_income(r):
                cat = r.get("category") or "other"
                by_category[cat] = _to_money(by_category.get(cat, 0) + _to_money(r["amount"]))

        # Recent itemized ledger (already ordered newest-first above).
        recent = []
        for r in rows[:limit]:
            recent.append({
                "date": r.get("date") or r.get("occurred_on"),
                "vendor": r.get("vendor") or "Unknown",
                "amount": _to_money(r["amount"]),
                "currency": r.get("currency") or "NGN",
                "category": r.get("category") or "other",
                "type": "income" if _is_income(r) else "expense",
                "source": "ledger",
            })

        # --- Merge in Gmail bank-alert data -----------------------------------
        # Never let a Gmail failure break the chat: fall back to ledger-only.
        gmail = {}
        g_res = get_gmail_financial_context(user_id, limit=30)
        if g_res["status"] == "success" and g_res["data"]:
            gmail = g_res["data"]

        total_income = _to_money(total_in + gmail.get("total_credits", 0))
        total_spending = _to_money(total_out + gmail.get("total_debits", 0))

        merged_by_category = dict(by_category)
        for cat, amt in (gmail.get("spending_by_category") or {}).items():
            merged_by_category[cat] = _to_money(merged_by_category.get(cat, 0) + amt)

        # The bank's latest alert balance wins when present (truth anchor);
        # otherwise fall back to the computed ledger balance.
        bank_balance = gmail.get("bank_balance")
        effective_balance = bank_balance if bank_balance is not None else balance

        # Combined ledger, newest first across both sources.
        combined = recent + (gmail.get("recent_transactions") or [])
        combined.sort(key=lambda t: t.get("date") or "", reverse=True)
        combined = combined[:limit]

        return _ok({
            "summary": {
                "balance": effective_balance,
                "ledger_balance": balance,
                "bank_balance": bank_balance,
                "bank_name": gmail.get("bank_name"),
                "last_alert_date": gmail.get("last_alert_date"),
                "total_income": total_income,
                "total_spending": total_spending,
                "transaction_count": len(rows) + gmail.get("alert_count", 0),
                "ledger_transaction_count": len(rows),
                "bank_alert_count": gmail.get("alert_count", 0),
                "spending_by_category": merged_by_category,
            },
            "recent_transactions": combined,
        })
    except Exception as e:
        print(f"[Database] get_chat_financial_context failed: {e}")
        return _err(e)


# ============================================
# GMAIL CREDENTIALS (one row per user; replaces the local ChromaDB collection)
# ============================================
def save_gmail_credentials(user_id: str, refresh_token: str = None,
                           access_token: str = None, token_expiry=None,
                           sender_domains=None) -> dict:
    """Upsert a user's Gmail OAuth credentials. Only non-None fields are written,
    so refreshing just the access token never wipes the stored refresh token."""
    row = {"user_id": user_id, "updated_at": datetime.now().isoformat()}
    if refresh_token is not None:
        row["refresh_token"] = refresh_token
    if access_token is not None:
        row["access_token"] = access_token
    if token_expiry is not None:
        row["token_expiry"] = token_expiry
    if sender_domains is not None:
        row["sender_domains"] = sender_domains
    try:
        res = supabase.table("gmail_credentials").upsert(
            row, on_conflict="user_id").execute()
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        print(f"[Database] save_gmail_credentials failed: {e}")
        return _err(e)


def get_gmail_credentials(user_id: str) -> dict:
    """Return the stored Gmail credential row for one user, or None."""
    try:
        res = (supabase.table("gmail_credentials").select("*")
               .eq("user_id", user_id).limit(1).execute())
        return _ok(res.data[0] if res.data else None)
    except Exception as e:
        return _err(e)


def get_all_gmail_credentials() -> dict:
    """Return every stored Gmail credential row (for the server auto-sync loop)."""
    try:
        res = supabase.table("gmail_credentials").select("*").execute()
        return _ok(res.data or [])
    except Exception as e:
        return _err(e)


def delete_gmail_credentials(user_id: str) -> dict:
    try:
        res = supabase.table("gmail_credentials").delete().eq("user_id", user_id).execute()
        return _ok(res.data)
    except Exception as e:
        return _err(e)
