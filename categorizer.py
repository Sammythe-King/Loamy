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
    "Software & Subscriptions",
    "Business Tools",
    "Shopping",
    "Transfers & Cash",
    "Uncategorized",
]
UNCATEGORIZED = "Uncategorized"
SOFTWARE = "Software & Subscriptions"
BUSINESS_TOOLS = "Business Tools"
TECH_CATEGORIES = {SOFTWARE, BUSINESS_TOOLS}
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

TRANSFERS = "Transfers & Cash"

# Money moved between the user's OWN accounts. Never an expense and never income:
# it carries a net spending effect of zero everywhere. It is deliberately NOT in
# STANDARD_CATEGORIES, so the AI categorizer can't assign it; only the transfer
# matcher (transfers.py) or the user (chat / Review Queue) can.
INTERNAL_TRANSFER = "Internal Transfer"
_INTERNAL_TRANSFER_ALIASES = {
    "internal transfer", "internal transfers", "own account transfer",
    "own account transfers", "transfer between accounts", "account transfer",
}
_COERCE_BY_LOWER = {**_CANONICAL_BY_LOWER, **{a: INTERNAL_TRANSFER for a in _INTERNAL_TRANSFER_ALIASES}}


def is_internal_transfer(category) -> bool:
    """True when a category label means 'money moved between my own accounts'."""
    return str(category or "").strip().lower() in _INTERNAL_TRANSFER_ALIASES

# Obvious consumer merchants. An alert naming one of these is safe to
# auto-categorize; anything else that looks like a transfer is left for the user.
KNOWN_MERCHANTS = [
    "shoprite", "spar", "justrite", "maxcare", "carrefour", "super u", "ebeano", "prince ebeano",
    "market square", "hubmart", "mart",
    "uber", "bolt", "indrive", "rida", "taxify", "total energies", "totalenergies", "oando",
    "conoil", "mobil", "ardova", "air peace", "arik", "ibom air",
    "mtn", "airtel", "glo", "9mobile", "dstv", "gotv", "startimes", "multichoice", "ikedc",
    "ekedc", "aedc", "phed", "ibedc", "spectranet", "smile", "starlink",
    "netflix", "spotify", "apple.com", "itunes", "google", "youtube", "showmax", "prime video",
    "bet9ja", "sportybet", "betking", "filmhouse", "genesis cinema", "silverbird",
    "chowdeck", "glovo", "jumia food", "domino", "chicken republic", "kfc", "mr biggs",
    "sweet sensation", "tantalizers", "cold stone", "burger king", "pizza hut", "starbucks",
    "jumia", "konga", "amazon", "aliexpress", "temu", "shein", "slot", "zara", "h&m", "game store",
]

_MERCHANT_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(m) for m in KNOWN_MERCHANTS) + r")(?![a-z0-9])"
)

# Peer-to-peer / POS / generic debit alert markers (NIBSS FIP/NIP refs, mobile
# banking "MB:", "TRANSFER TO", inward credits, USSD, POS terminals).
_AMBIGUOUS_TRANSFER_RE = re.compile(
    r"(fip\s*:|nip\s*:|nip/|\bmb\s*:|mobile\s*banking|transfer|\btrf\b|\btrfr?\b|\btrx\b|"
    r"\bpos\b|cpwinward|inward|outward|nibss|ussd|\bto\s+[a-z]+\s+[a-z]+|"
    r"\bfrom\s+[a-z]+\s+[a-z]+|\bopay\b|\bpalmpay\b|\bmoniepoint\b|\bkuda\b)"
)


# Taxonomy guardrail: software and digital-tool vendors can only ever be filed
# under TECH_CATEGORIES. Gemini has tagged things like "Google Deepsearch AI" as
# Dining & Food, so this list decides, not the model. Cloud/work tooling goes to
# "Business Tools"; consumer AI apps and other software go to "Software & Subscriptions".
_BUSINESS_TOOL_VENDORS = [
    "aws", "amazon web services", "azure", "google cloud", "gcp", "cloudflare", "vercel",
    "netlify", "heroku", "digitalocean", "github", "gitlab", "bitbucket", "atlassian", "jira",
    "slack", "zoom", "notion", "figma", "canva", "adobe", "godaddy", "namecheap", "hostinger",
    "mailchimp", "hubspot", "quickbooks", "zoho", "twilio", "sendgrid", "supabase", "neon",
    "mongodb", "workspace",
]
_SOFTWARE_VENDORS = [
    "google", "gemini", "deepsearch", "deep search", "deepmind", "openai", "chatgpt",
    "anthropic", "claude", "midjourney", "perplexity", "cursor", "copilot", "grok", "xai",
    "microsoft", "msft", "dropbox", "icloud", "1password", "lastpass", "nordvpn",
    "expressvpn", "proton", "linkedin", "apple.com/bill",
]


def _vendor_re(names: list) -> "re.Pattern":
    return re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![a-z0-9])")


_BUSINESS_TOOL_RE = _vendor_re(_BUSINESS_TOOL_VENDORS)
# "Google Play" / "Google Pay" are not software subscriptions, so only the
# bare "google" token counts.
_SOFTWARE_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(
        re.escape(n) + (r"(?!\s*(?:play|pay))" if n == "google" else "") for n in _SOFTWARE_VENDORS
    ) + r")(?![a-z0-9])"
)


def tech_category(text: str):
    """The category a tech vendor must be filed under, or None if not a tech vendor."""
    lowered = str(text or "").lower()
    if _BUSINESS_TOOL_RE.search(lowered):
        return BUSINESS_TOOLS
    if _SOFTWARE_RE.search(lowered):
        return SOFTWARE
    return None


def enforce_tech_taxonomy(text: str, category: str, tx_type: str = "debit") -> str:
    """Final guardrail for any path that assigns a category (chat, receipts, queue
    replies): a tech vendor can only be filed under TECH_CATEGORIES."""
    if tx_type == "credit" or is_internal_transfer(category):
        return category
    forced = tech_category(text)
    if forced and category not in TECH_CATEGORIES:
        return forced
    return category


def has_known_merchant(text: str) -> bool:
    lowered = str(text or "").lower()
    return bool(_MERCHANT_RE.search(lowered)) or tech_category(lowered) is not None


def is_ambiguous_transfer(text: str) -> bool:
    return bool(_AMBIGUOUS_TRANSFER_RE.search(str(text or "").lower()))


def apply_guardrails(item: dict, result: dict) -> dict:
    """Never auto-file generic transfers. The user decides via the Review Queue.

    - "Transfers & Cash" is never a final AI answer.
    - A transfer/POS/ambiguous alert without an obvious consumer merchant is
      "Uncategorized", whatever Gemini guessed."""
    text = f"{item.get('vendor') or ''} {item.get('description') or ''}"
    category = result.get("category") or UNCATEGORIZED
    forced = tech_category(text) if (item.get("type") or "debit") != "credit" else None
    if forced:
        if category in TECH_CATEGORIES:
            return result
        return {"category": forced, "confidence": max(float(result.get("confidence") or 0), 0.9)}
    if category == TRANSFERS:
        return {"category": UNCATEGORIZED, "confidence": result.get("confidence", 0.0)}
    if category != UNCATEGORIZED and is_ambiguous_transfer(text) and not has_known_merchant(text):
        return {"category": UNCATEGORIZED, "confidence": result.get("confidence", 0.0)}
    return result

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
    if key in _COERCE_BY_LOWER:
        return _COERCE_BY_LOWER[key]
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
- Software, AI tools, apps and digital services (Google, Gemini, OpenAI, ChatGPT, Claude,
  Microsoft, Dropbox, VPNs) -> "Software & Subscriptions". Cloud, hosting and work tools
  (AWS, Azure, Vercel, GitHub, Slack, Zoom, Notion, Adobe) -> "Business Tools".
  A software or tech vendor must NEVER be filed as Dining & Food, Groceries, Shopping,
  Transportation or Utilities & Bills, whatever the amount or wording looks like.
- Retail, fashion, electronics, online stores -> "Shopping".
- Person-to-person transfers, NIP/FIP/MB transfers ("FIP:MB:...", "TRANSFER TO ..."),
  POS transfers, inward credits and any alert that does NOT name an obvious consumer
  merchant -> "Uncategorized". The user will label these themselves.
- Never guess a category from a person's name.
- Moving money between the user's own accounts is NOT spending. You cannot tell which
  accounts belong to the user, so never label a transfer as an expense category: answer
  "Uncategorized" and the app will match it to the user's other accounts itself.
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
        direct = apply_guardrails(item, {"category": UNCATEGORIZED, "confidence": 0.0})
        if direct["category"] in TECH_CATEGORIES:
            results[i] = direct
            continue
        cached = _cache.get(_cache_key(item))
        if cached:
            results[i] = cached
        else:
            pending.append(i)

    for start in range(0, len(pending), BATCH_SIZE):
        chunk_idx = pending[start:start + BATCH_SIZE]
        chunk_results = _categorize_chunk([items[i] for i in chunk_idx])
        for i, res in zip(chunk_idx, chunk_results):
            res = apply_guardrails(items[i], res)
            results[i] = res
            if res["category"] != UNCATEGORIZED:
                _cache[_cache_key(items[i])] = res

    return results


NOTE_EXPENSE_CATEGORIES = [
    "Groceries", "Dining & Food", "Transportation", "Utilities & Bills",
    "Entertainment & Subscriptions", "Software & Subscriptions", "Business Tools",
    "Shopping", "Bank Charges", "Personal Expenses", "Business Expenses",
    "Family & Gifts", "Loan Repayment", "Transfers & Cash",
]
NOTE_INCOME_CATEGORIES = [
    "Business Income", "Personal Income", "Salary", "Loan Repayment Received",
    "Gifts & Support Received", "Refund",
]
_NOTE_CANONICAL = {
    c.lower(): c for c in NOTE_EXPENSE_CATEGORIES + NOTE_INCOME_CATEGORIES + [INTERNAL_TRANSFER]
}

# Catches "intertransfer", "inter-transfer", "internal transfer", "between my accounts",
# "my other account", "moved it to my wema account", including common misspellings.
_INTERNAL_NOTE_RE = re.compile(
    r"(inter[\s-]?trans|internal[\s-]?trans|own[\s-]?account|between\s+(my|our)\s+accounts?|"
    r"(my|me)\s+(other|own|second)\s+(bank\s+)?(account|bank)|"
    r"(moved?|sent|transferred?)\s+(it\s+)?(to|from)\s+my\s+(\w+\s+){0,2}(account|bank|wallet))",
    re.IGNORECASE,
)


def note_means_internal_transfer(note) -> bool:
    return bool(_INTERNAL_NOTE_RE.search(str(note or "")))


def interpret_note(vendor, amount, tx_type, note) -> dict:
    """Understand a free-text note about ONE transaction and file it.

    Returns {"category": <label or None>, "reply": <short message>, "unrelated": <bool>}.
    category is None when the note is too vague; reply is then a follow-up
    question and the item must stay open. unrelated is True when the message
    is not about this transaction at all (e.g. a general money question).
    Never raises."""
    is_credit = tx_type == "credit"
    if note_means_internal_transfer(note):
        return {
            "category": INTERNAL_TRANSFER,
            "reply": "Got it, that's money moved between your own accounts. I'll log it under Internal Transfers.",
            "unrelated": False,
        }
    allowed = (NOTE_INCOME_CATEGORIES if is_credit else NOTE_EXPENSE_CATEGORIES) + [INTERNAL_TRANSFER]
    direction = "money received (credit)" if is_credit else "money spent (debit)"
    model = _get_model()
    if model is None:
        return {"category": None, "reply": "I can't read notes right now. Please try again in a moment.", "unrelated": False}

    prompt = f"""You are Loamy, a friendly money assistant for a Nigerian small-business owner.
The user is explaining ONE bank transaction in their own words. Work out what it was and file it.

Transaction: {direction}, amount N{float(amount or 0):,.2f}, bank narration: "{vendor}"
User's note: "{note}"

Allowed categories (choose exactly one string from this list, nothing else):
{json.dumps(allowed)}

Guidance:
- Selling something personal (e.g. "pay for the shoes I sold") -> "Personal Income".
  Money from customers or sales of business stock/services -> "Business Income".
- Someone paying back money they owed (e.g. "repayment from my brother") -> "Loan Repayment Received".
- Gifts, allowance or help from family/friends -> "Gifts & Support Received".
- Wages -> "Salary". A reversed or returned payment -> "Refund".
- For spending: personal non-business things -> "Personal Expenses"; business costs -> "Business Expenses";
  money given to family or friends -> "Family & Gifts"; paying back a loan -> "Loan Repayment".
- Money moved between the user's own accounts or wallets -> "Internal Transfer".
- Several messages may be joined with " | " (oldest first); read them together as one explanation.
- If the note truly does not say what the money was for, set category to null and ask ONE short
  follow-up question in "reply".
- If the message is clearly NOT about this transaction (for example a general question about their
  finances), set category to null and "unrelated" to true.

Return ONLY JSON: {{"category": "<allowed category or null>", "unrelated": false, "reply": "<one friendly sentence confirming what you logged, or the follow-up question>"}}"""

    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json", "temperature": 0.2},
        )
        parsed = json.loads(response.text or "{}")
    except Exception as e:
        print(f"[categorizer] interpret_note failed: {e}")
        return {"category": None, "unrelated": False,
                "reply": "I couldn't quite understand that. Could you say a bit more about it?"}

    raw = str(parsed.get("category") or "").strip().lower()
    category = _NOTE_CANONICAL.get(raw)
    if category and category not in allowed:
        category = None
    reply = str(parsed.get("reply") or "").strip()
    unrelated = bool(parsed.get("unrelated")) and not category
    if not category and not reply:
        reply = "What was this money for?"
    return {"category": category, "reply": reply, "unrelated": unrelated}


def categorize_one(amount, vendor, description, tx_type="debit") -> dict:
    return categorize_batch([{
        "amount": amount, "vendor": vendor, "description": description, "type": tx_type,
    }])[0]
