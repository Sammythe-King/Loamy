"""
review_actions.py
Shared logic for answering a Review Queue item in plain language.

Both the Review Queue mini chat (routes.py) and the main chat / WhatsApp
(chat_service.py) call resolve_item(), so a note like "repayment from my
brother" is understood and logged the same way everywhere. This module also
lets the chat bring up an unanswered item ("Also, I wanted to ask...") and
treat the user's next message as the answer.
"""

import re
import uuid
from datetime import datetime, timedelta

import categorizer
import database
import transfers
from email_parser import _is_real_user_id, _normalize_vendor
from models import gmail_data_collection, review_queue_collection, vendor_memory_collection

ASK_COOLDOWN = timedelta(hours=6)
ANSWER_WINDOW = timedelta(minutes=30)
_INTROS = ("Also, I wanted to ask:", "One more thing, I wanted to clarify:")


def _ok(data):
    return {"status": "success", "data": data, "error": None}


def _err(message):
    return {"status": "error", "data": None, "error": message}


def _naira(value) -> str:
    return f"\u20a6{float(value or 0):,.0f}"


def _tag_source_transaction(meta: dict, category: str) -> None:
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


def _remember_vendor(meta: dict, category: str) -> None:
    try:
        vendor = meta.get("vendor", "")
        vendor_key = _normalize_vendor(vendor)
        payload = {"vendor_key": vendor_key, "vendor": vendor, "category": category,
                   "user_id": meta.get("user_id", "default")}
        existing = vendor_memory_collection.get(where={"vendor_key": vendor_key})
        if existing["ids"]:
            vendor_memory_collection.update(ids=[existing["ids"][0]], metadatas=[payload])
        else:
            vendor_memory_collection.add(
                ids=[f"vm_{uuid.uuid4().hex[:12]}"], documents=[f"{vendor} = {category}"], metadatas=[payload])
    except Exception as e:
        print(f"[REVIEW] Vendor memory save error: {e}")


_NO_RE = re.compile(r"^\s*(no|nope|nah)\b|not\s+(a\s+)?transfer|isn'?t\s+(a\s+)?transfer", re.IGNORECASE)
_BARE_NO_WORDS = {"no", "nope", "nah", "not", "a", "an", "it", "its", "it's", "is", "isn't", "was", "wasn't",
                  "transfer", "thanks", "please", "that", "this", "that's"}
_BARE_YES_WORDS = {"yes", "yeah", "yep", "yup", "yh", "y", "correct", "sure", "right", "it", "is", "was",
                   "that", "this", "that's", "thats", "please", "indeed"}
_ACCOUNT_RE = re.compile(
    r"\b(?:to|from|into|in)\s+(?:my\s+)?([A-Za-z][A-Za-z0-9&]*(?:\s[A-Za-z0-9&]+)?)\s+(?:account|acct|bank)\b",
    re.IGNORECASE,
)
_GENERIC_ACCOUNT_WORDS = {"other", "another", "second", "own", "my", "same", "first", "new", "old"}


def _only_words(text: str, allowed: set) -> bool:
    words = re.findall(r"[A-Za-z']+", str(text or "").lower())
    return bool(words) and all(w in allowed for w in words)


def _destination_from(note: str, meta: dict) -> str:
    """The other account named in the note, else what the matcher already knew."""
    match = _ACCOUNT_RE.search(note or "")
    if match:
        name = match.group(1).strip()
        if name.lower() not in _GENERIC_ACCOUNT_WORDS:
            return name if not name.islower() else name.title()
    return meta.get("counterpart_bank") or meta.get("destination_account") or "Another account of mine"


def _file_as_transfer(review_id: str, meta: dict, note: str) -> dict:
    """Close the item (and its other side, when matched) as an Internal Transfer."""
    res = transfers.confirm_transfer_pair(review_id)
    if res["status"] != "success":
        res = transfers.set_transfer_destination(review_id, _destination_from(note, meta))
    if res["status"] != "success":
        return res
    return _ok({
        "resolved": True, "category": categorizer.INTERNAL_TRANSFER, "vendor": meta.get("vendor", ""),
        "moved_to": "transfers",
        "confirmation": f"Got it. Logged {_naira(meta.get('amount'))} as a transfer between your own accounts. "
                        f"It's under Internal Transfers and won't count as spending.",
    })


def _resolve_pending_transfer(review_id: str, meta: dict, reply: str, history) -> dict:
    """A transfer card in the Review Queue answered in plain language: yes/which
    account files it as an Internal Transfer, anything else sends it back to be
    understood as a normal expense or income."""
    note = " | ".join([*(h for h in (history or []) if h), reply])
    is_credit = meta.get("transaction_type") == "credit"

    if _NO_RE.search(reply):
        transfers.reject_transfer(review_id)
        if _only_words(reply, _BARE_NO_WORDS):
            ask = "who it was from" if is_credit else "what it was for"
            return _ok({"resolved": False, "unrelated": False, "moved_to": "expenses",
                        "reply": f"No problem, I've kept it as a normal transaction. Can you tell me {ask}?"})
        res = resolve_item(review_id, reply, history=history)
        if res["status"] == "success" and not (res["data"] or {}).get("resolved"):
            res["data"]["moved_to"] = "expenses"
        return res

    if categorizer.note_means_internal_transfer(note) or _only_words(reply, _BARE_YES_WORDS):
        return _file_as_transfer(review_id, meta, note)

    understood = categorizer.interpret_note(
        meta.get("vendor", ""), meta.get("amount", 0), meta.get("transaction_type") or "debit", note)
    category = understood.get("category")
    if not category:
        return _ok({"resolved": False, "unrelated": bool(understood.get("unrelated")),
                    "reply": understood.get("reply") or "Is this money moved between your own accounts, or what was it for?"})
    if categorizer.is_internal_transfer(category):
        return _file_as_transfer(review_id, meta, note)
    transfers.reject_transfer(review_id)
    return resolve_item(review_id, reply, category=category, history=history)


def resolve_item(review_id: str, reply: str = "", category: str = "", history=None) -> dict:
    """Understand `reply` (plus earlier messages in `history`) and file the item.

    Data shape: {"resolved": bool, "reply": follow-up question} when still open,
    {"resolved": True, "category", "moved_to", "confirmation"} when filed."""
    reply = (reply or "").strip()
    category = (category or "").strip()
    if category and not reply:
        reply = category
    if not review_id or not reply:
        return _err("review_id and reply are required")

    result = review_queue_collection.get(ids=[review_id])
    if not result["ids"]:
        return _err("Review item not found")
    meta = result["metadatas"][0]
    vendor = meta.get("vendor", "")
    ai_reply = ""

    if transfers.is_transfer_item(meta) and meta.get("status") == "pending":
        return _resolve_pending_transfer(review_id, meta, reply, history)

    if not category:
        note = " | ".join([*(h for h in (history or []) if h), reply])
        understood = categorizer.interpret_note(
            vendor, meta.get("amount", 0), meta.get("transaction_type") or "debit", note)
        category = understood.get("category")
        ai_reply = understood.get("reply") or ""
        if not category:
            return _ok({"resolved": False, "unrelated": bool(understood.get("unrelated")),
                        "reply": ai_reply or "What was this money for?"})

    guarded = categorizer.enforce_tech_taxonomy(
        f"{vendor} {reply}", category, meta.get("transaction_type") or "debit")
    if guarded != category:
        category, ai_reply = guarded, ""

    if categorizer.is_internal_transfer(category):
        transfers.stage_as_transfer(review_id, meta, reply)
        return _file_as_transfer(review_id, meta, reply)

    meta.update({"status": "resolved", "user_reply": reply, "category": category,
                 "resolved_at": datetime.now().isoformat()})
    review_queue_collection.update(ids=[review_id], metadatas=[meta])
    _tag_source_transaction(meta, category)
    _remember_vendor(meta, category)

    confirmation = ai_reply or f"Got it. Logged {_naira(meta.get('amount'))} under {category}."
    return _ok({"resolved": True, "category": category, "vendor": vendor,
                "moved_to": "", "confirmation": confirmation})


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _pending_expenses(user_id: str) -> list:
    rq = review_queue_collection.get(where={"user_id": user_id})
    return [
        (rid, meta) for rid, meta in zip(rq.get("ids", []), rq.get("metadatas", []))
        if (meta or {}).get("status") == "pending" and not transfers.is_transfer_item(meta)
    ]


def _question_for(rid: str, meta: dict) -> str:
    intro = _INTROS[sum(map(ord, rid)) % len(_INTROS)]
    amount, vendor, date = _naira(meta.get("amount")), meta.get("vendor") or "an unknown sender", meta.get("date") or ""
    when = f" on {date}" if date else ""
    if meta.get("transaction_type") == "credit":
        return (f"{intro} {amount} came in from {vendor}{when}. Who or what was it from? "
                f"(e.g. repayment from my brother, or pay for the shoes I sold)")
    return (f"{intro} I noticed a payment of {amount} to {vendor}{when}. What was it for? "
            f"(e.g. stock, supplies, a bill, or lunch)")


def next_question(user_id: str) -> str:
    """The next unanswered item phrased for chat, marked as asked. "" when none."""
    if not _is_real_user_id(user_id):
        return ""
    try:
        now = datetime.now()
        candidates = []
        for rid, meta in _pending_expenses(user_id):
            asked = _parse_time(meta.get("chat_asked_at"))
            if asked and now - asked < ASK_COOLDOWN:
                continue
            if float(meta.get("amount") or 0) <= 0:
                continue
            candidates.append((meta.get("created_at", ""), rid, meta))
        if not candidates:
            return ""
        _, rid, meta = max(candidates, key=lambda c: c[0])
        meta["chat_asked_at"] = now.isoformat()
        review_queue_collection.update(ids=[rid], metadatas=[meta])
        return _question_for(rid, meta)
    except Exception as e:
        print(f"[REVIEW] could not build chat question: {e}")
        return ""


def _awaiting_item(user_id: str):
    now = datetime.now()
    best = None
    for rid, meta in _pending_expenses(user_id):
        asked = _parse_time(meta.get("chat_asked_at"))
        if asked and now - asked < ANSWER_WINDOW and (best is None or asked > best[0]):
            best = (asked, rid, meta)
    return (best[1], best[2]) if best else None


def try_answer_from_chat(user_id: str, message: str):
    """If Loamy just asked about a transaction in chat, treat `message` as the
    answer. Returns the reply to send, or None when the message is something
    else and normal chat should handle it."""
    if not _is_real_user_id(user_id) or not (message or "").strip():
        return None
    try:
        awaiting = _awaiting_item(user_id)
        if not awaiting:
            return None
        rid, meta = awaiting
        earlier = [h for h in str(meta.get("chat_history") or "").split(" | ") if h]
        res = resolve_item(rid, message, history=earlier)
        data = res.get("data") or {}
        if res["status"] != "success":
            return None
        if data.get("resolved"):
            follow_up = next_question(user_id)
            return data["confirmation"] + (f"\n\n{follow_up}" if follow_up else "")
        if data.get("unrelated"):
            return None
        meta["chat_history"] = " | ".join([*earlier, message.strip()])[-600:]
        meta["chat_asked_at"] = datetime.now().isoformat()
        review_queue_collection.update(ids=[rid], metadatas=[meta])
        return data.get("reply")
    except Exception as e:
        print(f"[REVIEW] chat answer failed: {e}")
        return None
