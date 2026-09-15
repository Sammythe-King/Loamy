"""
chat_service.py
Standalone AI chat helper for Loamy.

Single responsibility: take one user message, build user-scoped financial
context from Supabase, ask Gemini, persist the turn, and return the reply.
Keeps all AI-chat logic out of main.py (Swap-and-Plug + Bridge rules).
"""

import os
import secrets
from datetime import datetime

import google.generativeai as genai
from dotenv import load_dotenv

import database

load_dotenv()

# --- Model config (self-contained so this module can be swapped independently) ---
_API_KEY = os.getenv("GEMINI_API_KEY")
if not _API_KEY:
    raise ValueError("Missing GEMINI_API_KEY. Add it to your .env file.")
genai.configure(api_key=_API_KEY)

_model = genai.GenerativeModel("gemini-2.5-flash")

_ADVISOR_PERSONA = """You are Loamy, a friendly and precise financial advisor for an
African fintech app. You help users understand their cash flow, transactions, invoices,
and expense tracking. Use the financial data provided below to answer accurately.

Rules:
- Currency is Nigerian Naira (₦) unless a transaction says otherwise.
- Never invent numbers. Only use the figures in the context. If something isn't there,
  say you don't have that data yet.
- Be concise, warm, and practical. Give actionable advice grounded in the real numbers.
- For "how much did I spend today / this week / this month", quote the pre-computed
  figures in the "SPENDING THIS PERIOD" block EXACTLY. Never re-add the transaction
  list yourself for these totals - the block already accounts for both receipts and
  bank alerts. You may still list individual transactions as supporting detail.
- Focus exclusively on cash flow, transactions, invoices, and expense tracking. Do not
  suggest or discuss savings goals, even if the user's data contains goal information.
"""

# Appended to the persona only for the WhatsApp channel. WhatsApp renders a
# stray asterisk literally instead of bolding it, so headers/emphasis have to
# come from uppercase text and a fixed emoji/bullet set instead of markdown.
_WHATSAPP_FORMAT_RULES = """
WHATSAPP MESSAGE FORMATTING (this reply is sent as a plain WhatsApp text message):
- NEVER use the asterisk character (*) anywhere in your reply - no markdown bold (*text*), no
  asterisk bullet points, no asterisk emphasis of any kind.
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


def _format_context(ctx: dict) -> str:
    """Turn the structured context dict into a clean text payload for Gemini.

    Covers BOTH sources: the Supabase ledger (receipts/manual entries) and
    parsed Gmail bank alerts, so balance questions can be answered exactly.
    """
    summary = ctx.get("summary", {}) or {}
    recent = ctx.get("recent_transactions", []) or []

    by_cat = summary.get("spending_by_category", {}) or {}
    cat_lines = "\n".join(
        f"  - {cat}: ₦{amt:,.2f}" for cat, amt in by_cat.items()
    ) or "  - No categorized spending yet."

    ledger_lines = "\n".join(
        f"  - {t.get('date')}: {t.get('vendor')} — ₦{t.get('amount', 0):,.2f} "
        f"({t.get('category')}, {t.get('type')}, via {t.get('source', 'ledger')})"
        for t in recent
    ) or "  - No transactions recorded yet."

    # Balance block: be explicit about which figure is the bank's truth anchor.
    bank_balance = summary.get("bank_balance")
    if bank_balance is not None:
        balance_lines = (
            f"- Bank balance (latest alert from {summary.get('bank_name') or 'bank'}"
            f" on {summary.get('last_alert_date') or 'unknown date'}): ₦{bank_balance:,.2f}"
            "  <-- authoritative; use this when asked 'what is my balance'\n"
            f"- Balance from app-logged records only: ₦{summary.get('ledger_balance', 0):,.2f}"
        )
    else:
        balance_lines = (
            f"- Current balance (from app-logged records): "
            f"₦{summary.get('balance', 0):,.2f}\n"
            "- No bank alert balance available yet."
        )

    counts = (f"{summary.get('transaction_count', 0)} total "
              f"({summary.get('ledger_transaction_count', 0)} logged, "
              f"{summary.get('bank_alert_count', 0)} bank alerts)")

    # Authoritative, pre-computed period spend. These are calculated
    # deterministically in code across BOTH receipts and bank alerts, so the AI
    # must quote them verbatim for "today / this week / this month" questions
    # instead of trying to add up the dated list itself.
    period_lines = (
        f"- Spent TODAY ({summary.get('today_date', 'today')}): "
        f"₦{summary.get('spent_today', 0):,.2f}  <-- use this exact figure for 'spent today'\n"
        f"- Spent THIS WEEK (since Monday): ₦{summary.get('spent_this_week', 0):,.2f}\n"
        f"- Spent THIS MONTH: ₦{summary.get('spent_this_month', 0):,.2f}"
    )

    return f"""=== FINANCIAL SUMMARY ===
{balance_lines}
- Total income (receipts + bank credits): ₦{summary.get('total_income', 0):,.2f}
- Total spending (receipts + bank debits): ₦{summary.get('total_spending', 0):,.2f}
- Records on file: {counts}

=== SPENDING THIS PERIOD (authoritative, already computed - quote exactly) ===
{period_lines}

=== SPENDING BY CATEGORY ===
{cat_lines}

=== RECENT TRANSACTIONS (most recent first, both sources) ===
{ledger_lines}
"""


def build_financial_snapshot(user_id: str) -> str:
    """Public helper: the combined Gmail + ledger snapshot as prompt-ready text.

    Exposed so every channel (web chat, WhatsApp, future SMS) feeds Gemini the
    exact same numbers instead of each one rebuilding its own context.
    Returns "" when context can't be read, so callers can degrade gracefully.
    """
    res = database.get_chat_financial_context(user_id)
    if res["status"] != "success" or not res.get("data"):
        print(f"[chat_service] snapshot unavailable for {user_id}: {res.get('error')}")
        return ""
    return _format_context(res["data"])


def handle_chat_turn(user_id: str, message: str, chat_id: str = None, channel: str = "web") -> str:
    """Process one chat turn end-to-end and return the AI reply string.

    Steps: fetch user-scoped context -> prompt Gemini -> persist user+AI messages.
    Defensive: any failure returns a friendly message instead of raising.
    `channel` defaults to "web"; pass "whatsapp" to switch the reply to the
    plain-text, no-asterisk WhatsApp house style.
    """
    if not user_id or not message:
        return "I need both a user and a message to help you."

    # 1. Fetch user-scoped financial context from Supabase.
    ctx_res = database.get_chat_financial_context(user_id)
    if ctx_res["status"] != "success":
        print(f"[chat_service] context fetch failed: {ctx_res['error']}")
        return "I'm having trouble reading your financial data right now. Please try again shortly."
    context_text = _format_context(ctx_res["data"])

    # 2. Ask Gemini with persona + context + the user's question.
    try:
        persona = _ADVISOR_PERSONA + (_WHATSAPP_FORMAT_RULES if channel == "whatsapp" else "")
        prompt = f"{persona}\n\n{context_text}\n\nUser question: {message}\n\nYour answer:"
        response = _model.generate_content(prompt)
        reply = (response.text or "").strip() or "I'm not sure how to answer that yet."
    except Exception as e:
        print(f"[chat_service] Gemini call failed: {e}")
        return "I'm having trouble thinking right now. Please try again in a moment."

    # 3. Persist the turn onto a conversation thread (best-effort; never blocks).
    try:
        cid = chat_id or f"chat_{secrets.token_hex(8)}"
        existing = database.get_chat(cid)
        prior = []
        if existing["status"] == "success" and existing["data"]:
            prior = existing["data"].get("messages") or []
        prior.append({"sender": "user", "content": message})
        prior.append({"sender": "ai", "content": reply})
        if existing["status"] == "success" and existing["data"]:
            database.update_chat(cid, messages=prior)
        else:
            database.add_chat(user_id=user_id, chat_id=cid,
                              title=message[:50], messages=prior)
    except Exception as e:
        print(f"[chat_service] failed to persist chat turn: {e}")

    return reply
