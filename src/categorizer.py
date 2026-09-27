"""
categorizer.py
AI expense categorization for Loamy.

Single responsibility: map a transaction (amount, vendor, description) onto one
of Loamy's standard categories using Gemini. No storage, no HTTP - callers in
the ingestion pipeline decide where the result is saved.

Anything Gemini can't place confidently comes back as "Uncategorized", which the
dashboard already treats as "needs review", so it lands in the Review Queue.
"""

import json
import os
import re
import threading

import google.generativeai as genai

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

STANDARD_CATEGORIES = [
    "Groceries",
    "Dining & Food",
    "Transportation",
    "Utilities & Bills",
    "Entertainment & Subscriptions",
    "Shopping",
    "Transfers & Cash",
    "Uncategorized",
]
UNCATEGORIZED = "Uncategorized"
MIN_CONFIDENCE = 0.7
BATCH_SIZE = 25

_LEGACY_ALIASES = {
    "food": "Dining & Food",
    "dining": "Dining & Food",
    "restaurant": "Dining & Food",
    "grocery": "Groceries",
    "transport": "Transportation",
    "travel": "Transportation",
    "bills": "Utilities & Bills",
    "utilities": "Utilities & Bills",
    "airtime": "Utilities & Bills",
    "entertainment": "Entertainment & Subscriptions",
    "subscriptions": "Entertainment & Subscriptions",
    "purchase": "Shopping",
    "transfer": "Transfers & Cash",
    "transfers": "Transfers & Cash",
    "cash withdrawal": "Transfers & Cash",
    "other": UNCATEGORIZED,
    "banking": UNCATEGORIZED,
    "": UNCATEGORIZED,
}

_CANONICAL_BY_LOWER = {c.lower(): c for c in STANDARD_CATEGORIES}

_model = None
_model_lock = threading.Lock()
# Same vendor + direction almost always means the same category, so a repeat
# (e.g. weekly "MAXCARE MART") never costs another Gemini call this process.
_cache: dict = {}


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                return None
            genai.configure(api_key=api_key)
            _model = genai.GenerativeModel("gemini-2.5-flash")
        return _model


def coerce_category(value) -> str:
    """Snap any label (legacy, lowercase, AI output) onto the standard list."""
    key = str(value or "").strip().lower()
    if key in _CANONICAL_BY_LOWER:
        return _CANONICAL_BY_LOWER[key]
    return _LEGACY_ALIASES.get(key, UNCATEGORIZED)


def _cache_key(item: dict) -> str:
    vendor = re.sub(r"[^a-z0-9]", "", str(item.get("vendor") or item.get("description") or "").lower())
    return f"{vendor[:40]}|{item.get('type') or 'debit'}"


def _build_prompt(items: list) -> str:
    rows = "\n".join(
        json.dumps({
            "index": i,
            "amount": item.get("amount"),
            "type": item.get("type") or "debit",
            "vendor": item.get("vendor") or "",
            "description": (item.get("description") or "")[:200],
        }, ensure_ascii=False)
        for i, item in enumerate(items)
    )
    return f"""You categorize Nigerian bank transactions for a personal finance app.

Allowed categories (choose EXACTLY one string from this list, no others):
{json.dumps(STANDARD_CATEGORIES)}

Guidance:
- Supermarkets, marts, food stores -> "Groceries".
- Restaurants, fast food, food delivery (e.g. Chowdeck, Domino's) -> "Dining & Food".
- Uber, Bolt, fuel, bus, flights -> "Transportation".
- Electricity, DSTV/GOtv, airtime, data, internet, rent -> "Utilities & Bills".
- Netflix, Spotify, cinemas, betting, games -> "Entertainment & Subscriptions".
- Retail, fashion, electronics, online stores -> "Shopping".
- Person-to-person transfers, NIP/FIP transfers, ATM withdrawals, cash -> "Transfers & Cash".
- If you genuinely cannot tell, use "Uncategorized" with low confidence.

Give a confidence between 0 and 1 for each item.

Transactions (one JSON object per line):
{rows}

Return ONLY a JSON array with one object per input, in the same order:
[{{"index": 0, "category": "<one allowed category>", "confidence": 0.0}}]"""


def _categorize_chunk(items: list) -> list:
    fallback = [{"category": UNCATEGORIZED, "confidence": 0.0} for _ in items]
    model = _get_model()
    if model is None:
        return fallback
    try:
        response = model.generate_content(
            _build_prompt(items),
            generation_config={"response_mime_type": "application/json", "temperature": 0},
        )
        parsed = json.loads(response.text or "[]")
    except Exception as e:
        print(f"[categorizer] Gemini call failed: {e}")
        return fallback

    if not isinstance(parsed, list):
        return fallback

    results = list(fallback)
    for row in parsed:
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(items):
            continue
        raw = str(row.get("category") or "").strip()
        category = _CANONICAL_BY_LOWER.get(raw.lower())
        try:
            confidence = float(row.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not category or confidence < MIN_CONFIDENCE:
            category = UNCATEGORIZED
        results[idx] = {"category": category, "confidence": round(confidence, 2)}
    return results


def categorize_batch(items: list) -> list:
    """items: [{amount, vendor, description, type}] -> [{category, confidence}].

    Never raises. Errors, invalid labels and low confidence all resolve to
    "Uncategorized" so the transaction is routed to the Review Queue."""
    results = [None] * len(items)
    pending = []
    for i, item in enumerate(items):
        cached = _cache.get(_cache_key(item))
        if cached:
            results[i] = cached
        else:
            pending.append(i)

    for start in range(0, len(pending), BATCH_SIZE):
        chunk_idx = pending[start:start + BATCH_SIZE]
        chunk_results = _categorize_chunk([items[i] for i in chunk_idx])
        for i, res in zip(chunk_idx, chunk_results):
            results[i] = res
            if res["category"] != UNCATEGORIZED:
                _cache[_cache_key(items[i])] = res

    return results


def categorize_one(amount, vendor, description, tx_type="debit") -> dict:
    return categorize_batch([{
        "amount": amount, "vendor": vendor, "description": description, "type": tx_type,
    }])[0]
