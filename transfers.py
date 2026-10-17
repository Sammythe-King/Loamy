"""
transfers.py
Internal-transfer detection for Loamy.

Single responsibility: recognise money the user moved between their OWN
accounts so it is never counted as spending (or income).

  Tier 1  A debit and a credit on two different accounts with the same amount,
          a tight time window and matching references are linked silently as an
          "Internal Transfer" (resolved, never shown in the review queue).
  Tier 2  Anything less certain (only one side seen, ambiguous routing, a pair
          without matching references) is staged as a pending *transfer* item,
          shown in the Review Queue for the user to confirm. Only linked or
          confirmed transfers appear in the Internal Transfers tab.

Items are stored in the review queue collection with item_kind="transfer" so
every existing reader (category overrides, dashboards, chat context) picks the
"Internal Transfer" category up without a second source of truth.
"""

import hashlib
import re
from datetime import datetime

import categorizer
from models import review_queue_collection

KIND_TRANSFER = "transfer"
STAGE_LINKED = "linked"
STAGE_SINGLE = "single"
STAGE_CANDIDATE = "candidate"

AMOUNT_TOLERANCE = 0.01
TIGHT_WINDOW_MS = 2 * 60 * 60 * 1000
LOOSE_WINDOW_MS = 36 * 60 * 60 * 1000
MIN_REFERENCE_SCORE = 2

_STOPWORDS = {
    "NIP", "FIP", "TRANSFER", "TRF", "TRFR", "FROM", "THE", "AND", "FOR", "BANK", "MOBILE",
    "BANKING", "PAYMENT", "ALAT", "NONE", "WEB", "POS", "USSD", "INWARD", "OUTWARD", "NIBSS",
    "REF", "CHARGE", "CHARGES", "FEE", "ACCOUNT", "TXN", "TXNS", "CREDIT", "DEBIT", "ALERT",
    "PURCHASE", "SENT", "RECEIVED",
}


def _is_bank_alert(email: dict) -> bool:
    return email.get("is_bank_alert") is True or email.get("is_bank_alert") == "true"


def _amount_of(email: dict) -> float:
    try:
        return float(email.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _narration_of(email: dict) -> str:
    return str(email.get("narration") or email.get("subject") or "Transaction")


def source_id_of(email: dict) -> str:
    """Same id the review scan and chat categorizer use for a bank alert."""
    return email.get("id") or f"{_narration_of(email).lower()[:20]}_{_amount_of(email)}"


def _account_key(email: dict) -> str:
    bank = str(email.get("bank_slug") or email.get("bank_name") or "bank").strip().lower()
    return f"{bank}|{email.get('account_tail') or ''}"


def _bank_label(email: dict) -> str:
    return str(email.get("bank_name") or email.get("bank_slug") or "Bank")


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[A-Za-z]{3,}", str(text or "").upper()) if t not in _STOPWORDS}


def _overlap(a: set, b: set) -> int:
    """Distinct tokens of `a` that appear in `b`, tolerating truncation
    ("IYAL" vs "IYALLA") once at least four characters line up."""
    hits = 0
    for token in a:
        for other in b:
            if token == other or (min(len(token), len(other)) >= 4 and (token.startswith(other) or other.startswith(token))):
                hits += 1
                break
    return hits


def _long_refs(text: str) -> set:
    return set(re.findall(r"\d{8,}", str(text or "")))


def _bank_hint(narration_tokens: set, other_bank: str) -> bool:
    first = re.sub(r"[^A-Za-z]", "", str(other_bank or "").split(" ")[0]).upper()
    if len(first) < 3:
        return False
    return any(len(t) >= 3 and (first.startswith(t) or t.startswith(first)) for t in narration_tokens)


def _time_gap_ms(a: dict, b: dict):
    ams, bms = int(a.get("internal_date_ms") or 0), int(b.get("internal_date_ms") or 0)
    if ams > 0 and bms > 0:
        return abs(ams - bms)
    try:
        da = datetime.strptime(str(a.get("date") or "")[:10], "%Y-%m-%d")
        db = datetime.strptime(str(b.get("date") or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return abs((da - db).total_seconds()) * 1000


def _reference_score(debit: dict, credit: dict) -> int:
    d_tokens, c_tokens = _tokens(_narration_of(debit)), _tokens(_narration_of(credit))
    score = _overlap(d_tokens, c_tokens)
    if _long_refs(_narration_of(debit)) & _long_refs(_narration_of(credit)):
        score += 2
    if _bank_hint(d_tokens, _bank_label(credit)) or _bank_hint(c_tokens, _bank_label(debit)):
        score += 1
    return score


def _names_user(email: dict, user_tokens: set) -> bool:
    if len(user_tokens) < 2:
        return False
    return _overlap(user_tokens, _tokens(_narration_of(email))) >= 2


def find_transfer_matches(emails: list, user_name: str = "") -> dict:
    """Pure matching. Returns {"linked": [(debit, credit)], "candidates":
    [(debit, credit)], "singles": [email]} over bank alerts only."""
    alerts = [e for e in emails if _is_bank_alert(e) and _amount_of(e) > 0]
    debits = [e for e in alerts if (e.get("transaction_type") or "debit") != "credit"]
    credits_by_amount: dict = {}
    for credit in (e for e in alerts if e.get("transaction_type") == "credit"):
        credits_by_amount.setdefault(round(_amount_of(credit), 2), []).append(credit)

    options = []
    for debit in debits:
        amount = round(_amount_of(debit), 2)
        for key in (amount, round(amount + 0.01, 2), round(amount - 0.01, 2)):
            for credit in credits_by_amount.get(key, []):
                if _account_key(debit) == _account_key(credit):
                    continue
                if abs(_amount_of(debit) - _amount_of(credit)) > AMOUNT_TOLERANCE:
                    continue
                gap = _time_gap_ms(debit, credit)
                if gap is None or gap > LOOSE_WINDOW_MS:
                    continue
                score = _reference_score(debit, credit)
                options.append({"debit": debit, "credit": credit, "gap": gap, "score": score,
                                "strong": gap <= TIGHT_WINDOW_MS and score >= MIN_REFERENCE_SCORE})

    strong_per_leg: dict = {}
    for option in options:
        if option["strong"]:
            for leg in (source_id_of(option["debit"]), source_id_of(option["credit"])):
                strong_per_leg[leg] = strong_per_leg.get(leg, 0) + 1

    options.sort(key=lambda o: (-o["score"], o["gap"]))
    used, linked, candidates = set(), [], []
    for option in options:
        d_id, c_id = source_id_of(option["debit"]), source_id_of(option["credit"])
        if d_id in used or c_id in used:
            continue
        used.update((d_id, c_id))
        ambiguous = strong_per_leg.get(d_id, 0) > 1 or strong_per_leg.get(c_id, 0) > 1
        if option["strong"] and not ambiguous:
            linked.append((option["debit"], option["credit"]))
        else:
            candidates.append((option["debit"], option["credit"]))

    user_tokens = _tokens(user_name)
    singles = [e for e in alerts if source_id_of(e) not in used and _names_user(e, user_tokens)]
    return {"linked": linked, "candidates": candidates, "singles": singles}


def _pair_id(a: dict, b: dict) -> str:
    raw = "|".join(sorted((source_id_of(a), source_id_of(b))))
    return f"pair_{hashlib.sha1(raw.encode()).hexdigest()[:12]}"


def _naira(value) -> str:
    return f"\u20a6{float(value or 0):,.0f}"


def pending_question(meta: dict) -> str:
    """The question a pending transfer card shows. Always 'what was it for' /
    'who was it from', the same as an expense card, so cards stored with older
    yes/no wording read the same. A card the user already answered keeps its
    follow-up question."""
    if meta.get("user_reply"):
        return meta.get("nudge") or ""
    amount = _naira(meta.get("amount"))
    bank = meta.get("bank_name") or ""
    date = str(meta.get("date") or "")
    when = f" on {date}" if date else ""
    where = f" {bank}" if bank else ""
    if meta.get("transfer_stage") == STAGE_CANDIDATE:
        return (f"{amount} left one of your accounts and {amount} arrived in another around {date}. "
                f"What was this money for?")
    if meta.get("transaction_type") == "credit":
        return f"{amount} arrived in your{where} account{when}. Who or what was it from? (e.g. repayment from my brother, or pay for the shoes I sold)"
    return f"{amount} left your{where} account{when}. What was it for? (e.g. stock, supplies, a bill, or lunch)"


def _leg_meta(user_id: str, email: dict, partner, stage: str, pair_id: str, status: str) -> dict:
    amount = _amount_of(email)
    tx_type = "credit" if email.get("transaction_type") == "credit" else "debit"
    bank = _bank_label(email)
    other_bank = _bank_label(partner) if partner else ""
    date = str(email.get("date") or "")
    if stage == STAGE_LINKED:
        nudge = f"Auto-matched: {_naira(amount)} moved between your own accounts."
        vendor = f"Transfer {bank} to {other_bank}" if tx_type == "debit" else f"Transfer {other_bank} to {bank}"
    elif stage == STAGE_CANDIDATE:
        nudge = (f"{_naira(amount)} left one of your accounts and {_naira(amount)} arrived in another around "
                 f"{date}. What was this money for?")
        vendor = f"{bank} / {other_bank}"
    elif tx_type == "debit":
        nudge = (f"{_naira(amount)} left your {bank} account{' on ' + date if date else ''}. What was it for?")
        vendor = _narration_of(email)[:60]
    else:
        nudge = (f"{_naira(amount)} arrived in your {bank} account{' on ' + date if date else ''}. Who or what "
                 f"was it from?")
        vendor = _narration_of(email)[:60]
    now = datetime.now().isoformat()
    return {
        "user_id": user_id, "vendor": vendor[:60], "amount": amount, "transaction_type": tx_type,
        "date": date, "source_id": source_id_of(email), "status": status, "nudge": nudge,
        "user_reply": "Auto-matched internal transfer" if status == "resolved" else "",
        "category": categorizer.INTERNAL_TRANSFER if status == "resolved" else "",
        "created_at": date or now, "resolved_at": now if status == "resolved" else "",
        "item_kind": KIND_TRANSFER, "transfer_stage": stage, "pair_id": pair_id,
        "counterpart_source_id": source_id_of(partner) if partner else "",
        "counterpart_bank": other_bank, "bank_name": bank, "destination_account": "",
    }


def _upsert_leg(existing: dict, meta: dict, resolve: bool) -> bool:
    """Write one leg. Never overrides a user's own decision."""
    found = existing.get(meta["source_id"])
    try:
        if not found:
            review_queue_collection.add(
                ids=[f"rev_{meta['source_id']}"],
                documents=[f"Transfer: {meta['vendor']} {meta['amount']}"], metadatas=[meta],
            )
            return True
        review_id, current = found
        current = current or {}
        if current.get("transfer_rejected"):
            return False
        status = current.get("status")
        if status == "pending" or (resolve and status == "dismissed"):
            merged = {**current, **meta, "created_at": current.get("created_at") or meta["created_at"]}
            review_queue_collection.update(ids=[review_id], metadatas=[merged])
            return True
    except Exception as e:
        print(f"[TRANSFERS] could not write {meta['source_id']}: {e}")
    return False


def _leg_blocks_link(existing: dict, email: dict) -> bool:
    """A leg the user already resolved as a normal expense is left alone."""
    found = existing.get(source_id_of(email))
    if not found:
        return False
    meta = found[1] or {}
    if meta.get("transfer_rejected"):
        return True
    return meta.get("status") == "resolved" and not categorizer.is_internal_transfer(meta.get("category"))


def _load_existing(user_id: str) -> dict:
    try:
        rq = review_queue_collection.get(where={"user_id": user_id})
    except Exception as e:
        print(f"[TRANSFERS] could not read review queue: {e}")
        return {}
    return {
        (meta or {}).get("source_id"): (rid, meta)
        for rid, meta in zip(rq.get("ids", []), rq.get("metadatas", []))
        if (meta or {}).get("source_id")
    }


def _resolve_user_name(user_id: str, hint: str = "") -> str:
    if hint and hint.strip().lower() != "there":
        return hint
    try:
        import database
        res = database.get_user_by_id(user_id)
        row = res.get("data") if isinstance(res, dict) else None
        if isinstance(row, list):
            row = row[0] if row else None
        return str((row or {}).get("full_name") or "")
    except Exception as e:
        print(f"[TRANSFERS] could not read user name: {e}")
        return ""


def reconcile_internal_transfers(user_id: str, emails: list, user_name: str = "") -> dict:
    """Detect transfers in the synced alerts and persist them. Idempotent."""
    counts = {"linked": 0, "staged": 0}
    matches = find_transfer_matches(emails, _resolve_user_name(user_id, user_name))
    if not any(matches.values()):
        return counts
    existing = _load_existing(user_id)

    for debit, credit in matches["linked"]:
        if _leg_blocks_link(existing, debit) or _leg_blocks_link(existing, credit):
            continue
        pair = _pair_id(debit, credit)
        wrote = [
            _upsert_leg(existing, _leg_meta(user_id, debit, credit, STAGE_LINKED, pair, "resolved"), True),
            _upsert_leg(existing, _leg_meta(user_id, credit, debit, STAGE_LINKED, pair, "resolved"), True),
        ]
        counts["linked"] += sum(wrote)

    for debit, credit in matches["candidates"]:
        if _leg_blocks_link(existing, debit) or _leg_blocks_link(existing, credit):
            continue
        pair = _pair_id(debit, credit)
        for leg, partner in ((debit, credit), (credit, debit)):
            if source_id_of(leg) in existing and (existing[source_id_of(leg)][1] or {}).get("status") != "pending":
                continue
            counts["staged"] += _upsert_leg(
                existing, _leg_meta(user_id, leg, partner, STAGE_CANDIDATE, pair, "pending"), False)

    for email in matches["singles"]:
        if _leg_blocks_link(existing, email):
            continue
        found = existing.get(source_id_of(email))
        if found and (found[1] or {}).get("status") != "pending":
            continue
        counts["staged"] += _upsert_leg(
            existing, _leg_meta(user_id, email, None, STAGE_SINGLE, "", "pending"), False)

    print(f"[TRANSFERS] user {user_id}: linked {counts['linked']} leg(s), staged {counts['staged']}")
    return counts


def is_transfer_item(meta: dict) -> bool:
    return (meta or {}).get("item_kind") == KIND_TRANSFER


def _pair_members(review_id: str, meta: dict) -> list:
    members = [(review_id, meta)]
    pair = meta.get("pair_id")
    if not pair:
        return members
    try:
        peers = review_queue_collection.get(where={"pair_id": pair})
    except Exception as e:
        print(f"[TRANSFERS] pair lookup failed: {e}")
        return members
    for rid, peer in zip(peers.get("ids", []), peers.get("metadatas", [])):
        if rid != review_id and (peer or {}).get("user_id") == meta.get("user_id"):
            members.append((rid, peer))
    return members


def _close_as_transfer(members: list, reply: str, destination: str = "") -> int:
    now = datetime.now().isoformat()
    for rid, meta in members:
        meta.update({"status": "resolved", "category": categorizer.INTERNAL_TRANSFER,
                     "user_reply": reply, "resolved_at": now, "transfer_stage": STAGE_LINKED})
        if destination:
            meta["destination_account"] = destination
        review_queue_collection.update(ids=[rid], metadatas=[meta])
    return len(members)


def confirm_transfer_pair(review_id: str) -> dict:
    result = review_queue_collection.get(ids=[review_id])
    if not result["ids"]:
        return {"status": "error", "data": None, "error": "Review item not found"}
    meta = result["metadatas"][0]
    members = _pair_members(review_id, meta)
    if len(members) < 2:
        return {"status": "error", "data": None,
                "error": "No matching other side yet. Select the destination account instead."}
    _close_as_transfer(members, "Confirmed as a transfer pair")
    return {"status": "success", "error": None, "data": {"category": categorizer.INTERNAL_TRANSFER,
            "confirmation": f"Linked {_naira(meta.get('amount'))} as an internal transfer. It won't count as spending."}}


def set_transfer_destination(review_id: str, destination: str) -> dict:
    destination = (destination or "").strip()
    if not destination:
        return {"status": "error", "data": None, "error": "Choose an account first."}
    result = review_queue_collection.get(ids=[review_id])
    if not result["ids"]:
        return {"status": "error", "data": None, "error": "Review item not found"}
    meta = result["metadatas"][0]
    members = _pair_members(review_id, meta)
    _close_as_transfer(members, f"Moved to {destination}", destination)
    return {"status": "success", "error": None, "data": {"category": categorizer.INTERNAL_TRANSFER,
            "confirmation": f"Logged {_naira(meta.get('amount'))} as a transfer to {destination}. It won't count as spending."}}


def stage_as_transfer(review_id: str, meta: dict, note: str = "") -> None:
    """The user said an expense-queue item is money moved between their own
    accounts: keep it in the Review Queue as a transfer card to pick the other account."""
    amount = meta.get("amount")
    if meta.get("transaction_type") == "credit":
        nudge = f"Got it. Which of your accounts did the {_naira(amount)} come from?"
    else:
        nudge = f"Got it. Which of your accounts did the {_naira(amount)} go to?"
    meta.update({
        "item_kind": KIND_TRANSFER, "transfer_stage": STAGE_SINGLE, "pair_id": "",
        "transfer_rejected": False, "status": "pending", "category": "",
        "user_reply": note, "resolved_at": "", "nudge": nudge,
    })
    for key in ("counterpart_source_id", "counterpart_bank", "bank_name", "destination_account"):
        meta.setdefault(key, "")
    review_queue_collection.update(ids=[review_id], metadatas=[meta])


def reject_transfer(review_id: str) -> dict:
    """The user says this is a real expense/income: send it to the Expense Queue
    and make sure the matcher never stages it again."""
    result = review_queue_collection.get(ids=[review_id])
    if not result["ids"]:
        return {"status": "error", "data": None, "error": "Review item not found"}
    meta = result["metadatas"][0]
    for rid, member in _pair_members(review_id, meta):
        member.update({"item_kind": "expense", "transfer_stage": "", "pair_id": "", "transfer_rejected": True,
                       "status": "pending", "category": "", "user_reply": "",
                       "nudge": f"Hey, what was the {_naira(member.get('amount'))} "
                                f"{'received from' if member.get('transaction_type') == 'credit' else 'payment to'} "
                                f"{member.get('vendor')} for?"})
        review_queue_collection.update(ids=[rid], metadatas=[member])
    return {"status": "success", "data": {"id": review_id}, "error": None}
