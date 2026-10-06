"""
chat_tools.py
Gemini function calling + conversational state for the Loamy advisor.

Fixes the "yes please" loop: when Loamy offers to go through uncategorized
transactions and the user agrees, the model is forced to call
get_uncategorized_transactions instead of regenerating the spending summary.

Public entry point: generate_reply(model, prompt, user_id, user_message).
"""

import json
import re
import threading
import time
from datetime import date, datetime, timedelta

import database
from categorizer import STANDARD_CATEGORIES, UNCATEGORIZED, coerce_category
from vector_store import get_collection

try:
    from google.generativeai import protos as _protos
except ImportError:  # older google-generativeai releases
    import google.ai.generativelanguage as _protos


STATE_IDLE = "IDLE"
STATE_AWAITING = "AWAITING_CATEGORIZATION_APPROVAL"
STATE_IN_PROGRESS = "CATEGORIZATION_IN_PROGRESS"

STATE_TTL_SECONDS = 30 * 60
MAX_TOOL_ROUNDS = 4

_UNCATEGORIZED_LABELS = {"", "uncategorized", "unknown", "none", "null"}
_INCOME_TYPES = {"in", "income", "credit", "deposit"}

_review_queue = get_collection("review_queue")
_gmail_data = get_collection("gmail_data")
_vendor_memory = get_collection("vendor_memory")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _is_uncategorized(category) -> bool:
    return str(category or "").strip().lower() in _UNCATEGORIZED_LABELS


def _is_income(row: dict) -> bool:
    return str(row.get("transaction_type") or "").lower() in _INCOME_TYPES


def _row_date(row: dict):
    raw = row.get("occurred_on") or row.get("date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _user_transactions(user_id: str, limit: int = 1000) -> list[dict]:
    res = (database.supabase.table("transactions")
           .select("id,amount,currency,vendor,description,category,transaction_type,"
                   "occurred_on,date,source,created_at")
           .eq("user_id", user_id)
           .order("occurred_on", desc=True)
           .order("created_at", desc=True)
           .limit(limit).execute())
    return res.data or []


def _normalize_category(raw: str) -> str | None:
    """Snap to the standard list when possible; otherwise accept a short custom
    label (e.g. "Stock") the way the Review Queue already does. Returns None
    for empty / "uncategorized" input."""
    label = re.sub(r"\s+", " ", str(raw or "")).strip()
    if _is_uncategorized(label):
        return None
    canonical = coerce_category(label)
    if canonical != UNCATEGORIZED:
        return canonical
    if len(label) > 40:
        return None
    return label.title()


# ---------------------------------------------------------------------------
# Tool 1: get_uncategorized_transactions
# ---------------------------------------------------------------------------