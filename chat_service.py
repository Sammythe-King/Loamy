"""
chat_service.py

The Gemini-powered agentic advisor: conversation state / history helpers,
financial-snapshot context building, the main /chat handler (goal-setting,
manual-transaction detection, currency correction, and general Q&A), and the
WhatsApp advisor + receipt handlers registered with the whatsapp transport.
"""
import os
import re
import json
import asyncio
import secrets
from datetime import datetime, timedelta
from calendar import monthrange

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

import database
import categorizer
import chat_tools
from whatsapp import WHATSAPP_FORMAT_RULES, set_ai_handler, set_receipt_handler
from models import (
    model,
    goals_collection,
    accounts_collection,
    expenses_collection,
    chats_collection,
    invoices_collection,
    gmail_data_collection,
    pending_conversions_collection,
    review_queue_collection,
    vendor_memory_collection,
)
from business_logic import (
    to_sentence_case,
    compute_bank_snapshot,
    format_snapshot_for_ai,
    load_user_emails,
    get_exchange_rate,
    convert_to_local_currency,
    normalize_category,
)

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
- CURRENCY DISAMBIGUATION: The user spends in both Naira (NGN, ₦) and Mauritian Rupees
  (MUR, Rs). If they mention an amount with NO currency symbol, code, or word (e.g.
  "spent 600 on fuel"), do not assume silently. Either ask "Was this 600 MUR (Rupees)
  or 600 NGN (Naira)?" or state that you treated it as ₦ and that they can reply
  "600 MUR" to correct it.
- RECENT CONVERSATION covers every channel (WhatsApp and the web app). Treat it as one
  continuous conversation: if the user logged something on WhatsApp, you know about it here.
"""

from whatsapp import WHATSAPP_FORMAT_RULES as _WHATSAPP_FORMAT_RULES

HISTORY_CONTEXT_LIMIT = 15


def build_history_context(user_id: str, limit: int = HISTORY_CONTEXT_LIMIT) -> str:
    """Last `limit` messages for the user across ALL channels, as prompt text.
    Returns "" when there's no history (or the table isn't migrated yet)."""
    res = database.get_chat_history(user_id, limit=limit)
    if res["status"] != "success" or not res.get("data"):
        return ""
    lines = []
    for row in res["data"]:
        who = "User" if row.get("sender") == "user" else "Loamy"
        via = "WhatsApp" if row.get("channel") == "whatsapp" else "App"
        lines.append(f"[{via}] {who}: {(row.get('message') or '')[:800]}")
    return "\n".join(lines)


def record_chat_turn(user_id: str, user_message: str, reply: str,
                     channel: str = "app", user_at: datetime = None) -> bool:
    """Write a user message + assistant reply to the shared chat_history table.
    Best-effort: returns False instead of raising if the write fails."""
    res = database.add_chat_turn(user_id, user_message, reply, channel=channel, user_at=user_at)
    return res["status"] == "success"


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
    history_text = build_history_context(user_id)
    asked_at = datetime.utcnow()

    # 2. Ask Gemini with persona + context + cross-channel history + the question.
    try:
        persona = _ADVISOR_PERSONA + (_WHATSAPP_FORMAT_RULES if channel == "whatsapp" else "")
        history_block = (f"\n\n=== RECENT CONVERSATION (all channels, oldest first) ===\n{history_text}"
                         if history_text else "")
        prompt = f"{persona}\n\n{context_text}{history_block}\n\nUser question: {message}\n\nYour answer:"
        response = _model.generate_content(prompt)
        reply = (response.text or "").strip() or "I'm not sure how to answer that yet."
    except Exception as e:
        print(f"[chat_service] Gemini call failed: {e}")
        return "I'm having trouble thinking right now. Please try again in a moment."

    record_chat_turn(user_id, message, reply, channel=channel, user_at=asked_at)

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


async def chat_with_history(query: dict):
    user_msg = query.get("text")
    user_id = query.get("user_id", "default_user")  # Get user ID for Gmail data lookup
    # "web" (default) keeps the existing markdown-friendly formatting for the
    # web chat UI. "whatsapp" appends the plain-text formatting rules below,
    # since WhatsApp renders a stray asterisk literally instead of bolding it.
    channel = query.get("channel", "web")
    
    # Pre-calculate accurate date information so AI doesn't have to guess
    from calendar import monthrange
    now = datetime.now()
    current_date = now.strftime("%B %d, %Y")  # e.g., "April 08, 2026"
    current_day = now.day
    current_month = now.month
    current_year = now.year
    days_in_current_month = monthrange(current_year, current_month)[1]
    days_left_in_month = days_in_current_month - current_day
    
    # Build date context for AI
    date_context = f"""
    TODAY'S DATE: {current_date}
    CURRENT YEAR: {current_year}
    - Day of month: {current_day}
    - Days in {now.strftime('%B')}: {days_in_current_month}
    - Days remaining in {now.strftime('%B')}: {days_left_in_month} (from {now.strftime('%B')} {current_day + 1} to {now.strftime('%B')} {days_in_current_month})
    
    CRITICAL DATE RULE: When user mentions a date WITHOUT a year (e.g., "April 12th", "June 1st"):
    - ALWAYS default to CURRENT YEAR ({current_year}) or a FUTURE year
    - NEVER use a past year (like 2024 or 2025 if current year is {current_year})
    - If the date has already passed this year, use NEXT year
    """
    
    try:
        # --- NEW: Retrieve Recent Chat History ---
        # This gives the AI short-term memory of what it just proposed
        # Unified, cross-channel memory: the last 15 messages from chat_history
        # for THIS user, whether they came from WhatsApp or the web app.
        recent_chat_context = "No previous conversation in this session."
        asked_at = datetime.utcnow()
        try:
            unified = build_history_context(user_id)
            if unified:
                recent_chat_context = unified
        except Exception as e:
            print(f"Error fetching chat history: {e}")

        # 1. Get this user's spending ledger (receipts/manual entries) from
        #    Supabase. IMPORTANT: this used to read the ChromaDB `collection`,
        #    but nothing has written to that collection since receipts moved to
        #    Supabase (see database.add_transaction) - so a receipt scanned via
        #    WhatsApp or the web uploader could NEVER show up here, no matter
        #    how it was dated. It also compared dates as
        #    "September 11, 2026" == "2026-09-11", which never matches even
        #    when there WAS data. database.get_chat_financial_context() reads
        #    the real table and computes today/week/month totals deterministically
        #    (across receipts AND bank alerts) instead of leaving date math to the AI.
        ledger_ctx_res = database.get_chat_financial_context(user_id)
        ledger_data = (ledger_ctx_res.get("data") or {}) if ledger_ctx_res.get("status") == "success" else {}
        ledger_summary = ledger_data.get("summary", {}) or {}
        ledger_only_entries = [
            t for t in (ledger_data.get("recent_transactions") or [])
            if t.get("source") in ("receipt", "manual_cash")
        ]

        spending_entries = []
        for t in ledger_only_entries:
            amt = t.get("amount", 0) or 0
            cur = t.get("currency") or "NGN"
            symbol = "₦" if cur == "NGN" else cur + " "
            spending_entries.append(
                f"- {t.get('date')}: {t.get('vendor')} - {symbol}{amt:,.2f} ({t.get('category')})"
            )

        # Build comprehensive spending context. The SPENDING SUMMARY figures are
        # authoritative (combine receipts + bank alerts) - the AI should quote
        # them exactly rather than re-adding the itemized list itself.
        history_context = f"""
        === ALL TRANSACTIONS/RECEIPTS ===
        {chr(10).join(spending_entries) if spending_entries else "No transactions recorded."}
        
        === SPENDING SUMMARY (authoritative - quote exactly) ===
        - Total spent today ({ledger_summary.get('today_date', current_date)}): ₦{ledger_summary.get('spent_today', 0):,.2f}
        - Total spent this week (since Monday): ₦{ledger_summary.get('spent_this_week', 0):,.2f}
        - Total spent this month ({now.strftime('%B')} {current_year}): ₦{ledger_summary.get('spent_this_month', 0):,.2f}
        """
        
        # 2. Search for existing saving goals
        goal_results = goals_collection.query(query_texts=[user_msg], n_results=3)
        goal_docs = goal_results.get('documents', [[]])[0]
        goals_context = "\n".join(goal_docs) if goal_docs else "No active saving goals found."
        
        # 3. Get ALL goals AND bills with amounts for full context
        all_items = goals_collection.get()
        goals_summary = []
        bills_summary = []
        total_assigned = 0
        
        for i in range(len(all_items['ids'])):
            meta = all_items['metadatas'][i]
            item = meta.get('item', 'Unknown')
            amount = float(meta.get('amount', 0))
            assigned = float(meta.get('assigned', 0))
            deadline = meta.get('deadline', 'Not set')
            category = meta.get('category', 'goal')
            total_assigned += assigned
            
            entry = f"- {item}: Target ${amount:.2f}, Assigned ${assigned:.2f}, Deadline: {deadline}"
            if category == 'bill':
                bills_summary.append(entry)
            else:
                goals_summary.append(entry)
        
        goals_full_context = "\n".join(goals_summary) if goals_summary else "No savings goals set."
        bills_full_context = "\n".join(bills_summary) if bills_summary else "No bills tracked."
        
        # Combined context for AI to see everything
        all_items_context = f"""
        === SAVINGS GOALS ===
        {goals_full_context}
        
        === BILLS ===
        {bills_full_context}
        """
        
        # 4. Get THIS user's bank accounts from Supabase (scoped: the old
        #    unscoped ChromaDB read leaked every user's accounts into the prompt).
        accounts_summary = []
        total_balance = 0
        acct_res = database.get_accounts(user_id)
        if acct_res["status"] == "success":
            for acct in acct_res["data"]:
                name = acct.get('account_name') or acct.get('name', 'Unknown')
                balance = float(acct.get('balance', 0) or 0)
                total_balance += balance
                accounts_summary.append(f"- {name}: ₦{balance:,.2f}")
        
        ready_to_assign = total_balance - total_assigned
        accounts_context = "\n".join(accounts_summary) if accounts_summary else "No accounts found."
        accounts_context += f"\n\nTotal Balance: ₦{total_balance:,.2f}"
        accounts_context += f"\nTotal Assigned to Goals/Bills: ₦{total_assigned:,.2f}"
        accounts_context += f"\nReady to Assign (Available): ${ready_to_assign:.2f}"
        
        # 5. Get Gmail email data for spending insights
        gmail_context = ""
        bank_alerts_context = ""
        latest_bank_balance = None
        
        try:
            # Supabase-primary loader (ChromaDB fallback) so chat sees the same
            # emails as the dashboard.
            emails = load_user_emails(user_id)
            if emails:
                # Regular (non-bank-alert) email receipts
                regular_emails = [e for e in emails if not e.get('is_bank_alert')]

                # Format regular email receipts
                gmail_entries = []
                for email in regular_emails[:15]:
                    sender = email.get('sender', 'Unknown')
                    subject = email.get('subject', '')
                    date = email.get('date', '')
                    amount = email.get('amount')
                    email_type = email.get('type', 'receipt')
                    email_currency = email.get('currency', 'NGN')

                    # Auto-detect currency from sender for known USD vendors
                    sender_lower = sender.lower()
                    if any(usd_vendor in sender_lower for usd_vendor in ['vercel', 'aws', 'github', 'stripe', 'digitalocean', 'heroku', 'netlify', 'cloudflare', 'amazon web services']):
                        email_currency = 'USD'

                    if amount:
                        if email_currency != 'NGN':
                            # Foreign currency - show original + NGN equivalent
                            exchange_rate = get_exchange_rate(email_currency, "NGN")
                            ngn_equivalent = amount * exchange_rate
                            currency_symbols = {'USD': '$', 'EUR': '€', 'GBP': '£', 'MUR': '₨', 'INR': '₹'}
                            symbol = currency_symbols.get(email_currency, '$')
                            gmail_entries.append(f"- {date}: {sender} - {subject} ({symbol}{amount:,.2f} = ₦{ngn_equivalent:,.2f}, {email_type})")
                        else:
                            gmail_entries.append(f"- {date}: {sender} - {subject} (₦{amount:,.2f}, {email_type})")
                    else:
                        gmail_entries.append(f"- {date}: {sender} - {subject} ({email_type})")

                # Bank alerts: use the SHARED snapshot so the web chat,
                # WhatsApp, and the dashboard all report the SAME balance and
                # cash flow. (The old code picked the first alert that had a
                # balance, which is why chat disagreed with the dashboard.)
                snap = compute_bank_snapshot(user_id)
                latest_bank_balance = snap["balance"]

                if gmail_entries:
                    gmail_context = f"""
        === EMAIL RECEIPTS & PURCHASES FROM GMAIL ===
        {chr(10).join(gmail_entries)}
        """

                if snap["alert_count"] > 0:
                    bank_alerts_context = f"""
        === BANK TRANSACTION ALERTS FROM GMAIL ===
        The user's bank sends email alerts for every transaction. These are the
        VERIFIED numbers (identical to the dashboard) - use them exactly:

        {format_snapshot_for_ai(snap)}

        IMPORTANT: You have FULL ACCESS to the user's bank transactions above. When they ask about
        spending, income, or their balance - use this REAL data. Reference specific transactions
        when giving advice.
        """
        except Exception as e:
            print(f"Error loading Gmail context: {e}")
            gmail_context = ""
            bank_alerts_context = ""
        
        # 6. Detect if the user wants to create a goal AND has provided all required info
        # Auto-create when user provides item name + amount + deadline (no need to ask again)
        detection_prompt = f"""
        TODAY'S DATE: {current_date}
        CURRENT YEAR: {current_year}
        
        Analyze this conversation and current message:
        
        Recent Conversation:
        {recent_chat_context}
        
        Current Message: "{user_msg}"
        
        === ALL USER'S GOALS AND BILLS ===
        {all_items_context}
        
        Should we CREATE a new goal? Return is_goal: true if BOTH conditions are met:
        1. The user wants to save for something / set a goal (mentioned in conversation OR current message)
        2. ALL required info is now available (item name + amount + deadline) from conversation context
        
        RULES:
        - If user asked to set a goal AND has now provided amount + deadline = CREATE IT (is_goal: true)
        - Example: Conversation mentions "goal for skateboard", current message says "500 dollars by may 2nd" = CREATE IT
        - The item name must come from conversation context, NOT be a pronoun
        - If user says "it" or "the goal", look at conversation to find the actual item name
        - target_amount must be > 0
        - deadline must be a real date - use year {current_year} if no year specified
        - If the date has passed this year, use {current_year + 1}
        - Check if a similar goal already exists - if so, return is_goal: false (no duplicates)
        
        If ALL info available, return: {{"is_goal": true, "item": "Actual Item Name", "target_amount": 500.00, "deadline": "{current_year}-05-02"}}
        If info is missing or goal exists, return: {{"is_goal": false}}
        Return ONLY JSON.
        """
        detect_res = model.generate_content(detection_prompt, generation_config={"response_mime_type": "application/json"})
        goal_data = json.loads(detect_res.text)
        print(f"[v0] Goal detection result: {goal_data}")
        
        if goal_data.get("is_goal"):
            item_name = goal_data.get('item', '')
            target_amount = goal_data.get('target_amount', 0)
            deadline = goal_data.get('deadline', '')
            
            # Validate: Don't create if item is a pronoun or data is missing
            invalid_names = ['it', 'that', 'this', 'the goal', 'goal', 'one', '']
            if item_name.lower().strip() in invalid_names:
                print(f"Rejected goal creation - invalid name: {item_name}")
            elif target_amount <= 0 or not deadline or deadline == 'null':
                print(f"Rejected goal creation - missing data: amount={target_amount}, deadline={deadline}")
            else:
                # Check for duplicates
                is_duplicate = False
                all_existing = goals_collection.get()
                for i in range(len(all_existing['ids'])):
                    existing_item = all_existing['metadatas'][i].get('item', '').lower().strip()
                    if existing_item == item_name.lower().strip():
                        is_duplicate = True
                        break
                
                if not is_duplicate:
                    item_name = to_sentence_case(item_name)
                    goal_id = f"goal_{os.urandom(4).hex()}"
                    print(f"[v0] About to create goal: {item_name}, ${target_amount}, {deadline}, id={goal_id}")
                    goals_collection.add(
                        documents=[f"Goal: Save ${target_amount} for {item_name} by {deadline}"],
                        metadatas=[{"item": item_name, "amount": float(target_amount), "deadline": deadline, "assigned": 0, "category": "goal"}],
                        ids=[goal_id]
                    )
                    print(f"[v0] Goal successfully created: {item_name} - ${target_amount} by {deadline}")
                else:
                    print(f"Rejected goal creation - duplicate exists: {item_name}")

        # 5.5. --- NEW: Backend Auto-Allocation Execution ---
        # If the user is saying "yes" to a proposed allocation, parse and apply it here
        if recent_chat_context != "No previous conversation in this session.":
            try:
                confirm_prompt = f"""
                === CONVERSATION CONTEXT ===
                {recent_chat_context}
                
                === CURRENT MESSAGE ===
                "{user_msg}"
                
                === ALL USER'S GOALS AND BILLS ===
                {all_items_context}
                
                === TASK: DETECT FUND ALLOCATION (PUTTING MONEY TOWARDS A GOAL) ===
                
                IMPORTANT: ALLOCATION means FUNDING an EXISTING goal - adding money towards it.
                This is DIFFERENT from SETTING a goal's target amount.
                
                === WHAT IS ALLOCATION (return is_confirming: true) ===
                - "assign $500 to it" / "fund it with 500" / "allocate 500 to my couch"
                - "put 500 towards my goal" / "add money to it"
                - Confirming a proposed allocation (saying "yes" after AI proposed ALLOCATE: X | $Y)
                - The goal MUST ALREADY EXIST in the list above
                
                === WHAT IS NOT ALLOCATION (return is_confirming: false) ===
                - Setting up a NEW goal: "I want to save $500 for X by Y date"
                - Providing target amount for a goal being created: "215 dollars and I'd like it by april 29th"
                - If the conversation is about CREATING a goal and user provides amount + date = NOT allocation
                - If the goal does NOT exist yet in the list above = NOT allocation
                
                === CRITICAL RULE ===
                If the conversation shows the AI asking "what's the target amount?" or "how much does it cost?"
                and user responds with an amount = This is SETTING THE TARGET, NOT allocating funds.
                Return is_confirming: false in this case.
                
                === PRONOUN RESOLUTION ===
                When user says "it" or "the goal", look at conversation to find which existing goal they mean.
                The item MUST be an EXACT name from the goals list above.
                
                If ALLOCATING to an EXISTING goal, return: {{"is_confirming": true, "allocations": [{{"item": "Exact Goal Name", "amount": 500.00}}]}}
                If NOT allocating (setting up new goal, providing target, etc.), return: {{"is_confirming": false}}
                Return ONLY JSON.
                """
                confirm_res = model.generate_content(confirm_prompt, generation_config={"response_mime_type": "application/json"})
                confirm_data = json.loads(confirm_res.text)
                
                if confirm_data.get("is_confirming") and confirm_data.get("allocations"):
                    updated_items = []
                    all_goals_data = goals_collection.get()
                    
                    for alloc in confirm_data["allocations"]:
                        item_name = alloc.get("item", "").lower().strip()
                        add_amount = float(alloc.get("amount", 0))
                        
                        for i in range(len(all_goals_data['ids'])):
                            meta = all_goals_data['metadatas'][i]
                            db_item = meta.get('item', '').lower().strip()
                            # Flexible matching (e.g., "netflix" matches "Netflix")
                            if db_item == item_name or db_item in item_name or item_name in db_item:
                                goal_id = all_goals_data['ids'][i]
                                new_assigned = float(meta.get('assigned', 0)) + add_amount
                                
                                goals_collection.delete(ids=[goal_id])
                                goals_collection.add(
                                    documents=[all_goals_data['documents'][i]],
                                    metadatas=[{
                                        "item": meta.get('item'),
                                        "amount": meta.get('amount', 0),
                                        "deadline": meta.get('deadline', ''),
                                        "category": meta.get('category', 'goal'),
                                        "assigned": new_assigned
                                    }],
                                    ids=[goal_id]
                                )
                                updated_items.append(f"{meta.get('item')} (${add_amount:.2f})")
                                break
                    
                    if updated_items:
                        # Append system instruction so the AI knows it was successful and confirms to the user
                        user_msg += f"\n\n[SYSTEM NOTE: The system has successfully and automatically allocated funds to: {', '.join(updated_items)}. Tell the user the dashboard is updated and they should refresh the page to see the changes.]"
                        print(f"Backend Auto-Allocated: {updated_items}")
            except Exception as e:
                print(f"Auto-allocation detection failed: {e}")
        
        # 5.6. --- NEW: Backend Goal UPDATE Execution (for setting/changing target amount or deadline) ---
        if recent_chat_context != "No previous conversation in this session.":
            try:
                update_prompt = f"""
                TODAY'S DATE: {current_date}
                CURRENT YEAR: {current_year}
                
                Recent Conversation:
                {recent_chat_context}
                
                Current User Message: "{user_msg}"
                
=== ALL USER'S GOALS AND BILLS ===
                {all_items_context}
                
                Is the user asking to UPDATE an existing goal's TARGET AMOUNT or DEADLINE?
                
                === CONTEXT-AWARE PRONOUN RESOLUTION ===
                When user says "it", "that", "this", "the goal":
                1. Look at the CONVERSATION CONTEXT above
                2. Find what goal/bill was most recently discussed
                3. Use THAT item's EXACT name from the goals/bills list
                
                === INTENT DISAMBIGUATION ===
                "assign" can mean different things - PAY ATTENTION TO CONTEXT:
                - "assign $2000 to it" or "assign 500" (after discussing a goal) = ALLOCATE/FUND - return is_update: false
                - "assign $2000 as the TARGET" or "set target to $2000" or "change the amount to $2000" = UPDATE target - return is_update: true
                - "fund it", "allocate funds", "put money towards" = ALLOCATE/FUND - return is_update: false
                
                Only return is_update: true for SETTING/CHANGING the target amount or deadline, NOT for funding.
                
                === DATE RULES (CRITICAL) ===
                - If user says a date without a year (e.g., "April 12th"), use year {current_year} or later
                - NEVER use a past year like 2024 or 2025 if current year is {current_year}
                - If the date has already passed this year, use {current_year + 1}
                - Always return deadline in YYYY-MM-DD format
                
                === RULES ===
                - goal_name MUST be an EXACT name from the goals/bills list above
                - If user says "it", resolve to the actual goal name from conversation
                - amount = new target amount (0 if not being changed)
                - deadline = new deadline in YYYY-MM-DD (empty if not being changed)
                
                If updating TARGET/DEADLINE, return: {{"is_update": true, "goal_name": "Exact Name From List", "amount": 1850.00, "deadline": "{current_year}-05-23"}}
                If NOT updating (or if user wants to FUND/ALLOCATE), return: {{"is_update": false}}
                Return ONLY JSON.
                """
                update_res = model.generate_content(update_prompt, generation_config={"response_mime_type": "application/json"})
                update_data = json.loads(update_res.text)
                print(f"[v0] Goal update detection: {update_data}")
                
                if update_data.get("is_update"):
                    goal_name = update_data.get("goal_name", "").strip()
                    new_amount = update_data.get("amount", 0)
                    new_deadline = update_data.get("deadline", "")
                    
                    # Find and update the goal
                    all_goals_data = goals_collection.get()
                    goal_updated = False
                    
                    for i in range(len(all_goals_data['ids'])):
                        meta = all_goals_data['metadatas'][i]
                        db_item = meta.get('item', '').lower().strip()
                        
                        # Flexible matching
                        if db_item == goal_name.lower().strip() or goal_name.lower().strip() in db_item or db_item in goal_name.lower().strip():
                            goal_id = all_goals_data['ids'][i]
                            
                            # Build updated metadata
                            updated_meta = {
                                "item": meta.get('item'),
                                "amount": float(new_amount) if new_amount > 0 else float(meta.get('amount', 0)),
                                "deadline": new_deadline if new_deadline else meta.get('deadline', ''),
                                "category": meta.get('category', 'goal'),
                                "assigned": float(meta.get('assigned', 0))
                            }
                            
                            # Delete and re-add with updated data
                            goals_collection.delete(ids=[goal_id])
                            goals_collection.add(
                                documents=[f"Goal: Save ${updated_meta['amount']} for {updated_meta['item']} by {updated_meta['deadline']}"],
                                metadatas=[updated_meta],
                                ids=[goal_id]
                            )
                            
                            goal_updated = True
                            user_msg += f"\n\n[SYSTEM NOTE: Successfully updated {meta.get('item')} - Target: ${updated_meta['amount']}, Deadline: {updated_meta['deadline']}. Tell the user their goal has been updated and they should refresh the Savings Goals page.]"
                            print(f"[v0] Goal updated: {meta.get('item')} -> amount=${updated_meta['amount']}, deadline={updated_meta['deadline']}")
                            break
                    
                    if not goal_updated:
                        # Goal doesn't exist - CREATE it instead of failing
                        if new_amount > 0 and new_deadline:
                            item_name = to_sentence_case(goal_name)
                            goal_id = f"goal_{os.urandom(4).hex()}"
                            goals_collection.add(
                                documents=[f"Goal: Save ${new_amount} for {item_name} by {new_deadline}"],
                                metadatas=[{"item": item_name, "amount": float(new_amount), "deadline": new_deadline, "assigned": 0, "category": "goal"}],
                                ids=[goal_id]
                            )
                            user_msg += f"\n\n[SYSTEM NOTE: Created new goal '{item_name}' with target ${new_amount} by {new_deadline}. Tell the user their goal has been created and they should refresh the Savings Goals page.]"
                            print(f"[v0] Goal created (fallback): {item_name} - ${new_amount} by {new_deadline}")
                        else:
                            print(f"[v0] Goal update failed - could not find goal: {goal_name}")
                        
            except Exception as e:
                print(f"Goal update detection failed: {e}")

        # 6. Final Advisor Response (Grounded in History + Goals + Accounts + Context)
        advisor_prompt = f"""
        You are the Fintech AI Student Financial Advisor.
        {date_context}
        
        === USER'S BANK ACCOUNTS ===
        {accounts_context}
        
        === ALL SAVINGS GOALS ===
        {goals_full_context}
        
        === ALL BILLS ===
        {bills_full_context}
        
        === SPENDING HISTORY (Manual Entries) ===
        {history_context}
        
        {gmail_context}
        
        {bank_alerts_context}
        
        === CONVERSATION HISTORY (READ THIS CAREFULLY) ===
        {recent_chat_context}
        
        Current User Message: {user_msg}
        
        INSTRUCTIONS:
        - You have FULL ACCESS to the user's account balances shown above. When they ask about their balance, tell them!
        - The "Ready to Assign" amount is what they have available after subtracting money already assigned to goals/bills.
        - If the user just set a goal, confirm it and calculate how much they need to save daily/weekly to reach it by their deadline.
        - If they are asking for advice, look at their recent spending to see if they can afford their goals.
        - If user wants to allocate money (e.g., "allocate $2000 to my bills"), show them a breakdown of how you'd distribute it.
          CRITICAL: You MUST format each proposed allocation EXACTLY as a bulleted list using this syntax:
          * ALLOCATE: [Item Name] | $[Amount]
          Then ask: "Shall I update your dashboard with these changes?" - but do NOT actually make changes until they confirm.
        - If the Current User Message includes a [SYSTEM NOTE] saying allocations were successful, warmly inform the user that their dashboard has been updated and they can refresh their Savings Goals page to see it.
        - Keep the tone encouraging but realistic for a student.
        
        ### CONVERSATIONAL CONTEXT & MEMORY (CRITICAL):
        Before responding, ALWAYS scan the ENTIRE conversation history above to understand context:
        
        1. IDENTIFY THE ACTIVE SUBJECT: Track what goal/bill/item is currently being discussed.
           - When user mentions a specific item (e.g., "iPhone 17"), that becomes the ACTIVE SUBJECT
           - The ACTIVE SUBJECT remains until the user explicitly mentions a DIFFERENT item
           
        2. RESOLVE PRONOUNS & REFERENCES: When user says "it", "that", "this", "the goal", etc.:
           - ALWAYS replace with the ACTIVE SUBJECT from conversation history
           - Example: If discussing "iPhone 17" and user says "allocate $2000 to it" -> "allocate $2000 to iPhone 17"
           - NEVER create a goal/bill named "it", "that", "this", or any pronoun
           
        3. LINK RELATED INFORMATION: When user provides amount, date, or allocation in follow-up messages:
           - Connect it to the ACTIVE SUBJECT being discussed
           - Example: User says "I want an iPhone 17" then later "$8900 by May 23rd" -> iPhone 17 costs $8900, deadline May 23rd
           
        4. CHECK FOR DUPLICATES: Before creating ANY new goal:
           - Scan the Active Goals list for similar names (iPhone 17 = iphone 17 = Iphone17 = phone if discussing iPhone)
           - If a similar goal exists, UPDATE it instead of creating a new one
        
        ### GOAL CREATION RULES (MANDATORY):
        When user wants to create a new goal, you MUST collect this information BEFORE creating:
        
        1. REQUIRED - Goal Name: What are they saving for? (Must be specific, not a pronoun)
        2. REQUIRED - Target Date/Deadline: When do they want to reach this goal?
        3. RECOMMENDED - Target Amount: How much do they need?
        
        WORKFLOW:
        - If user provides goal name but NO date: ASK "When would you like to reach this goal?"
        - If user provides goal name but NO amount: ASK "How much does [goal name] cost?" OR offer "Would you like to set the amount later?"
        - If user says "just set it" or "I'll add amount later": You may create with amount=0, but date is STILL required
        - ONLY use the ALLOCATE format AFTER the goal exists and user wants to add money to it
        
        NEVER:
        - Create a goal with a pronoun as the name ("it", "that", "this")
        - Create duplicate goals for the same item
        - Create a goal without asking for the deadline first
        
        ### INVOICE CREATION RULES (MONEY OWED TO THE USER):
        An INVOICE tracks money that someone OWES the user (a client/customer), NOT money the user spent.
        Trigger phrases: "X owes me", "bill X for", "X needs to pay me", "set an invoice", "log an invoice",
        "create an invoice", "X is paying me later", "X will pay on [date]".

        Required fields to create an invoice:
        1. REQUIRED - Client Name: who owes the money (e.g., "Mr James", "Jimmy Ayo")
        2. REQUIRED - Amount: how much is owed (in NGN unless another currency is stated)
        3. OPTIONAL - Due Date: when it should be paid (e.g., "August 5"). Use current/future year only.
        4. OPTIONAL - Description: what it is for. Extract from phrasing like "owes me 15000 FOR groceries"
           -> description = "groceries". If no description is given, leave it blank and still create the invoice.

        WORKFLOW (CRITICAL):
        - If the user states an owed amount WITHOUT explicitly asking to set an invoice
          (e.g., "Mr James owes me 15,000 due August 5"), DO NOT create it immediately.
          Instead ASK: "Would you like me to set an invoice for this?" and wait for confirmation.
        - If the user explicitly says to set/create/log the invoice in the same message
          (e.g., "...set an invoice for it"), create it right away (no extra confirmation needed).
        - To ACTUALLY create the invoice, you MUST output a marker line in EXACTLY this format on its own line:
          * CREATE_INVOICE: [Client Name] | [Amount] | [Due Date or NONE] | [Description or NONE]
          Example: * CREATE_INVOICE: Mr James | 15000 | 2026-08-05 | groceries
          Example: * CREATE_INVOICE: Jimmy Ayo | 50000 | NONE | NONE
        - Only output the CREATE_INVOICE marker when you intend the invoice to be saved (either the user
          explicitly asked, OR they just confirmed "yes" to your "Would you like me to set an invoice?" question).
        - When you output the marker, also write a friendly confirmation sentence like
          "Done - I've logged an invoice for Mr James (₦15,000) due August 5."
        - Dates in the marker MUST be in YYYY-MM-DD format. If no date was given, use NONE.

        ### INTENT DISAMBIGUATION - "ASSIGN" AND SIMILAR WORDS:
        The word "assign" can mean different things - understand the user's TRUE intent:
        ALLOCATE/FUND (add money TOWARDS a goal - increases "assigned" amount):
        - "assign $2000 to it" / "assign 2000 dollars to my goal"
        - "fund it with $500" / "allocate $1000 towards my car"
        - "put $300 towards my vacation" / "add money to my savings"
        - Use the ALLOCATE format: * ALLOCATE: [Goal Name] | $[Amount]
        
        SET TARGET (change the goal's TARGET amount):
        - "assign $2000 AS the target" / "set the target to $2000"
        - "change the amount to $500" / "update target amount"
        - "the goal should be $3000" / "make it a $5000 goal"
        - This changes the goal's target_amount field
        
        SET DEADLINE (change the goal's deadline):
        - "assign a date of May 23rd" / "set the deadline to June 1st"
        - "change the date to April 15th" / "update the deadline"
        - This changes the goal's deadline field
        
        When ambiguous, consider context:
        - If goal already has a target and user says "assign $X to it" -> likely ALLOCATE (funding)
        - If goal has NO target ($0) and user says "assign $X to it" -> could be setting target, ASK to clarify
        
        ### DATE & TIME CALCULATIONS (CRITICAL - USE EXACT VALUES):
        - ALWAYS use the pre-calculated date values provided above. NEVER estimate or guess dates.
        - When user mentions a date WITHOUT a year, ALWAYS use the current year or a future year. NEVER use a past year.
        - When calculating days until a deadline:
          1. Use the "Days remaining in [month]" value from above for the current month
          2. Add full days for any complete months in between
          3. Add the day number for the target month
          Example: If today is April 8 and deadline is May 15:
            - Days left in April: Use the exact value above (NOT an estimate)
            - Days in May until 15th: 15
            - Total = Days left in April + 15
        - For daily/weekly savings: Divide the target amount by the EXACT number of days calculated
        - Month lengths: Jan=31, Feb=28(29 leap), Mar=31, Apr=30, May=31, Jun=30, Jul=31, Aug=31, Sep=30, Oct=31, Nov=30, Dec=31
        
        ### INFORMATION FILTERING & SCOPE (CRITICAL):
        Always analyze the user's intent to determine what information to show:
        
        1. IDENTIFY THE SUBJECT: Determine the specific financial noun the user is asking about.
           Examples: "Starbucks", "bills", "goals", "rent", "groceries", "Zenith Bank account"
        
        2. MATCH & FILTER based on specificity level:
           - SPECIFIC (e.g., "Show my Starbucks spending", "What about my Netflix bill?"):
             Filter to ONLY show data matching that specific item/merchant/name.
           - CATEGORICAL (e.g., "Show my goals", "What are my bills?", "Food expenses"):
             Filter to ONLY show items matching that category. 
             * "goals" = items with Category: goal (savings targets like new phone, vacation)
             * "bills" = items with Category: bill (recurring payments like Netflix, Rent)
             * Spending categories = filter transactions by that category keyword
           - BROAD (e.g., "How's my budget?", "Show everything", "Financial summary"):
             Provide a high-level summary across ALL relevant categories.
        
        3. LABEL CLEARLY: When showing multiple types of data, use clear headers:
           - "Your Bills:" for bill items
           - "Your Savings Goals:" for goal items  
           - "Recent Transactions:" for spending history
           - "Account Balances:" for account info
           This prevents confusion when displaying mixed data types.
        """

        if channel == "whatsapp":
            advisor_prompt += "\n" + WHATSAPP_FORMAT_RULES

        # Function calling + conversation state: a "yes" after Loamy offers to
        # categorize triggers get_uncategorized_transactions instead of a re-summary.
        reply_text = await asyncio.to_thread(
            chat_tools.generate_reply, model, advisor_prompt, user_id, user_msg
        )

        # --- Handle CREATE_INVOICE marker (AI-driven invoice creation) ---
        # The AI emits a line like: * CREATE_INVOICE: Mr James | 15000 | 2026-08-05 | groceries

        invoice_created = False
        try:
            import re
            from datetime import datetime as _dt
            import uuid as _uuid

            cleaned_lines = []
            for line in reply_text.split("\n"):
                marker_match = re.search(r"CREATE_INVOICE:\s*(.+)", line)
                if marker_match:
                    parts = [p.strip() for p in marker_match.group(1).split("|")]
                    if len(parts) >= 2 and parts[0] and parts[1]:
                        client_name = parts[0]
                        # Extract numeric amount
                        amount_raw = re.sub(r"[^\d.]", "", parts[1])
                        amount = float(amount_raw) if amount_raw else 0
                        due_date = ""
                        description = ""
                        if len(parts) >= 3 and parts[2].upper() != "NONE":
                            due_date = parts[2]
                        if len(parts) >= 4 and parts[3].upper() != "NONE":
                            description = parts[3]

                        if amount > 0:
                            invoice_id = f"inv_{_uuid.uuid4().hex[:12]}"
                            created_date = _dt.now().strftime("%Y-%m-%d")
                            invoices_collection.add(
                                ids=[invoice_id],
                                documents=[f"Invoice for {client_name}: {description} - {amount} NGN"],
                                metadatas=[{
                                    "user_id": user_id,
                                    "client_name": client_name,
                                    "amount": amount,
                                    "description": description,
                                    "due_date": due_date,
                                    "category": "Invoice",
                                    "status": "unpaid",
                                    "created_date": created_date,
                                    "paid_date": ""
                                }]
                            )
                            invoice_created = True
                            print(f"[INVOICE] Created from chat: {client_name} - {amount} NGN due {due_date}")
                    # Skip the marker line so it isn't shown to the user
                    continue
                cleaned_lines.append(line)

            reply_text = "\n".join(cleaned_lines).strip()
        except Exception as e:
            print(f"[INVOICE] Error parsing CREATE_INVOICE marker: {e}")

        # WhatsApp turns are recorded by whatsapp.py after delivery (it knows the
        # original message, not the snapshot-prefixed prompt), so only log web here.
        if channel != "whatsapp":
            await asyncio.to_thread(
                record_chat_turn, user_id, user_msg, reply_text, "app", asked_at
            )

        return {"reply": reply_text, "invoice_created": invoice_created}
        
    except Exception as e:
        import traceback
        print(f"Chat Error: {str(e)}")
        print(f"Full traceback: {traceback.format_exc()}")
        return {"reply": f"I'm having trouble accessing your financial profile right now. Debug info: {str(e)}"}


# --- WhatsApp AI bridge -------------------------------------------------------
# The ONE seam between the WhatsApp transport and the AI brain. whatsapp.py
# stays pure transport (it only knows a phone number); this bridge does the
# identity resolution and hands the AI a real, Supabase-backed user_id. Kept
# small and single-purpose (Logic/UI Separation + Atomic Modularity rules).

CURRENCY_SYMBOLS = {"NGN": "\u20a6", "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "INR": "\u20b9"}



def _resolve_tx_date(raw) -> str:
    today = datetime.now().date()
    text = str(raw or "").strip().lower()
    if not text or text == "today":
        return today.isoformat()
    if text == "yesterday":
        return (today - timedelta(days=1)).isoformat()
    parsed = database._to_date_obj(raw)
    return (parsed or today).isoformat()



def add_manual_transaction(user_id: str, amount, currency: str = "NGN", category: str = "",
                           description: str = "", date=None, source: str = "manual_cash") -> dict:
    """Write a user-reported expense into the unified transactions ledger.

    `amount` is in `currency`; the stored `amount` is always NGN, with the
    original figure kept alongside for foreign spends."""
    try:
        original_amount = float(re.sub(r"[^\d.]", "", str(amount)) or 0)
    except ValueError:
        original_amount = 0.0
    if original_amount <= 0:
        return {"status": "error", "data": None, "error": "Amount must be greater than zero."}

    currency = (currency or "NGN").strip().upper()[:3] or "NGN"
    conversion = convert_to_local_currency(original_amount, currency, "NGN")
    amount_ngn = conversion["converted_amount"]
    description = (description or "").strip() or "Cash expense"

    resolved_category = categorizer.coerce_category(category)
    if resolved_category == categorizer.UNCATEGORIZED:
        resolved_category = categorizer.categorize_one(amount_ngn, description, description)["category"]

    tx_date = _resolve_tx_date(date)
    tx_id = f"{'cash' if source == 'manual_cash' else 'trans'}_{os.urandom(6).hex()}"
    doc_text = f"Spent \u20a6{amount_ngn:,.2f} on {description} ({resolved_category}) on {tx_date}"
    if currency != "NGN":
        doc_text += f" (Original: {currency} {original_amount:,.2f})"

    save_res = database.add_transaction(
        user_id=user_id,
        tx_id=tx_id,
        amount=amount_ngn,
        vendor=description,
        description=description,
        original_amount=original_amount,
        original_currency=currency,
        currency="NGN",
        date=tx_date,
        category=resolved_category,
        transaction_type="expense",
        is_estimate=conversion["is_estimate"],
        document=doc_text,
        source=source,
    )
    if save_res["status"] != "success":
        return save_res
    return {"status": "success", "error": None, "data": {
        "id": tx_id, "amount_ngn": amount_ngn, "original_amount": original_amount,
        "original_currency": currency, "category": resolved_category,
        "description": description, "date": tx_date, "source": source,
    }}



def format_logged_amount(amount_ngn, original_amount=None, original_currency="NGN") -> str:
    text = f"\u20a6{float(amount_ngn or 0):,.0f}"
    if original_currency and original_currency != "NGN" and original_amount:
        symbol = CURRENCY_SYMBOLS.get(original_currency)
        original = f"{symbol}{float(original_amount):,.0f}" if symbol else f"{float(original_amount):,.0f} {original_currency}"
        text += f" ({original})"
    return text



MANUAL_TRANSACTION_TOOL = {
    "function_declarations": [{
        "name": "add_manual_transaction",
        "description": (
            "Log an expense the user tells you they made (cash, card or transfer) "
            "that should be added to their spending ledger."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "amount": {"type": "NUMBER", "description": "Amount spent, in `currency`."},
                "currency": {"type": "STRING", "description": "ISO 4217 code, e.g. NGN, MUR, USD. Default NGN."},
                "category": {"type": "STRING", "enum": categorizer.STANDARD_CATEGORIES},
                "description": {"type": "STRING", "description": "Merchant or what it was for, in Title Case."},
                "date": {"type": "STRING", "description": "Date of the spend as YYYY-MM-DD."},
            },
            "required": ["amount", "currency", "category", "description", "date"],
        },
    }]
}



def extract_manual_transaction_call(text: str):
    """Ask Gemini whether the message logs a spend; return its tool-call args or None."""
    today = datetime.now()
    prompt = (
        f"Today is {today.strftime('%A, %Y-%m-%d')}. You route WhatsApp messages for Loamy, "
        "a personal finance app. If the user is REPORTING money they already spent "
        "(e.g. 'I spent 1000 MUR on groceries yesterday', 'paid 3k for a taxi'), you MUST call "
        "add_manual_transaction, resolving relative dates to YYYY-MM-DD and shorthand like 3k to 3000. "
        "Currency: use the code the user states (₦/naira -> NGN, Rs/rupees -> MUR, $ -> USD). If the "
        "user gives NO currency, set currency to NGN (the base currency); the app will append a note "
        "letting them correct it.\n"
        "Questions about spending, goals, invoices or anything else must NOT call the tool.\n\n"
        f"User message: {text}"
    )
    response = model.generate_content(
        prompt,
        tools=[MANUAL_TRANSACTION_TOOL],
        tool_config={"function_calling_config": {"mode": "AUTO"}},
    )
    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            call = getattr(part, "function_call", None)
            if call and call.name == "add_manual_transaction":
                return {key: value for key, value in call.args.items()}
    return None



_CURRENCY_MENTION_RE = re.compile(
    r"(₦|\bngn\b|\bnaira\b|\bmur\b|\brs\.?(?=\s*\d)|\brupees?\b|\$|\busd\b|\bdollars?\b|"
    r"£|\bgbp\b|\bpounds?\b|€|\beur\b|\beuros?\b|\bkes\b|\bzar\b|\binr\b)",
    re.IGNORECASE,
)
_CURRENCY_CORRECTION_RE = re.compile(
    r"^\s*(?:it\s+was\s+)?([\d,]+(?:\.\d+)?)\s*(MUR|NGN|USD|EUR|GBP|rupees?|naira)\s*\.?\s*$",
    re.IGNORECASE,
)
_CURRENCY_WORDS = {"rupee": "MUR", "rupees": "MUR", "naira": "NGN"}
_CORRECTION_WINDOW_SECONDS = 30 * 60

# user_id -> the last expense logged with an assumed (not stated) currency, so a
# follow-up "600 MUR" can re-log it in the right currency.
_pending_currency_corrections: dict = {}


def has_explicit_currency(text: str) -> bool:
    return bool(_CURRENCY_MENTION_RE.search(text or ""))



async def _try_currency_correction(text: str, user_id: str):
    """Handle a reply like "600 MUR" that fixes the last ambiguous expense."""
    match = _CURRENCY_CORRECTION_RE.match(text or "")
    pending = _pending_currency_corrections.get(user_id)
    if not match or not pending:
        return None
    if time.time() - pending["logged_at"] > _CORRECTION_WINDOW_SECONDS:
        _pending_currency_corrections.pop(user_id, None)
        return None

    amount = float(match.group(1).replace(",", ""))
    currency_token = match.group(2).lower()
    currency = _CURRENCY_WORDS.get(currency_token, currency_token.upper())
    if abs(amount - pending["original_amount"]) > 0.01:
        return None
    if currency == pending["currency"]:
        _pending_currency_corrections.pop(user_id, None)
        return f"Got it - keeping it as {format_logged_amount(pending['amount_ngn'])}."

    result = await asyncio.to_thread(
        add_manual_transaction, user_id, amount, currency, pending["category"],
        pending["description"], pending["date"], "manual_cash",
    )
    if result["status"] != "success":
        return "I couldn't update that expense just now. Please try again in a moment."
    await asyncio.to_thread(database.delete_transaction, pending["tx_id"])
    _pending_currency_corrections.pop(user_id, None)

    tx = result["data"]
    return (
        "\u270d\ufe0f *Expense Updated!*\n"
        f"• *Merchant*: {tx['description']}\n"
        f"• *Amount*: {format_logged_amount(tx['amount_ngn'], tx['original_amount'], tx['original_currency'])}\n"
        f"• *Category*: {tx['category']}\n"
        "\n"
        "_Synced to your Loamy dashboard_"
    )



async def _try_log_manual_transaction(text: str, user_id: str):
    """Returns a confirmation card if the message was a spend report, else None."""
    if not re.search(r"\d", text or ""):
        return None
    corrected = await _try_currency_correction(text, user_id)
    if corrected:
        return corrected
    try:
        args = await asyncio.to_thread(extract_manual_transaction_call, text)
    except Exception as e:
        print(f"[WhatsApp bridge] manual-entry function call failed: {e}")
        return None
    if not args:
        return None

    result = await asyncio.to_thread(
        add_manual_transaction,
        user_id,
        args.get("amount"),
        args.get("currency") or "NGN",
        args.get("category") or "",
        args.get("description") or "",
        args.get("date"),
        "manual_cash",
    )
    if result["status"] != "success":
        print(f"[WhatsApp bridge] manual entry save failed: {result['error']}")
        return "I couldn't log that expense just now. Please try again in a moment."

    tx = result["data"]
    date_label = datetime.fromisoformat(tx["date"]).strftime("%b %d")
    footer = (
        "_Tap Review Queue in Loamy to pick a category_"
        if tx["category"] == categorizer.UNCATEGORIZED
        else "_Synced to your Loamy dashboard_"
    )

    currency_note = ""
    if not has_explicit_currency(text):
        amount_label = f"{tx['original_amount']:,.0f}"
        other = "NGN" if tx["original_currency"] == "MUR" else "MUR"
        _pending_currency_corrections[user_id] = {
            "tx_id": tx["id"], "original_amount": tx["original_amount"],
            "currency": tx["original_currency"], "amount_ngn": tx["amount_ngn"],
            "category": tx["category"], "description": tx["description"],
            "date": tx["date"], "logged_at": time.time(),
        }
        currency_note = f'\n_No currency given - if this was in {other}, reply "{amount_label} {other}" to update._'

    return (
        "\u270d\ufe0f *Expense Logged!*\n"
        f"• *Merchant*: {tx['description']}\n"
        f"• *Amount*: {format_logged_amount(tx['amount_ngn'], tx['original_amount'], tx['original_currency'])}\n"
        f"• *Category*: {tx['category']}\n"
        f"• *Date*: _{date_label}_\n"
        "\n"
        f"{footer}"
        f"{currency_note}"
    )



async def _whatsapp_ai_handler(text: str, from_phone: str) -> str:
    # A. Resolve the WhatsApp phone to a Loamy account (Phase 1: look up an
    #    existing user by their saved phone_number; full self-service linking
    #    comes in a later phase). DB work is blocking, so off-load to a thread.
    try:
        lookup = await asyncio.to_thread(database.get_user_by_phone, from_phone)
    except Exception as e:
        print(f"[WhatsApp bridge] Could not resolve user for {from_phone}: {e}")
        return "I couldn't reach your account right now. Please try again shortly."

    user_row = lookup.get("data") if lookup.get("status") == "success" else None
    if not user_row:
        return ("I couldn't find a Loamy account linked to this number yet. "
                "Open the Loamy web app, go to your Dashboard, and use "
                "\"Connect WhatsApp\" to link this number - then message me again.")
    user_id = user_row["user_id"]

    # B. "I spent 1000 MUR on groceries" -> Gemini function call -> ledger row.
    logged = await _try_log_manual_transaction(text, user_id)
    if logged:
        return logged

    # C. Give the advisor the SAME snapshot the web chat and dashboard use, from
    #    the shared compute_bank_snapshot() so every channel reports identical
    #    numbers. (Previously WhatsApp read an empty Supabase table -> ₦0.00.)
    snap = await asyncio.to_thread(compute_bank_snapshot, user_id)
    if snap.get("alert_count", 0) > 0 or snap.get("ledger_count", 0) > 0:
        text = (
            "=== VERIFIED FINANCIAL SNAPSHOT (use these exact numbers) ===\n"
            f"{format_snapshot_for_ai(snap)}\n"
            "=== END SNAPSHOT ===\n\n"
        ) + text

    result = await chat_with_history({"text": text, "user_id": user_id, "channel": "whatsapp"})
    return result.get("reply", "")


set_ai_handler(_whatsapp_ai_handler)



async def _whatsapp_receipt_handler(file_data: bytes, mime_type: str, from_phone: str) -> str:
    """Turn a receipt photo sent over WhatsApp into a saved expense.

    Same identity path as the text handler (phone -> Supabase user), then reuses
    the SHARED process_receipt_image() pipeline so a WhatsApp photo and a web
    upload land as identical dashboard entries. Returns the confirmation text
    whatsapp.py sends back to the user."""
    try:
        lookup = await asyncio.to_thread(database.get_user_by_phone, from_phone)
    except Exception as e:
        print(f"[WhatsApp receipt] user lookup failed for {from_phone}: {e}")
        return "I couldn't reach your account right now. Please try again shortly."

    user_row = lookup.get("data") if lookup.get("status") == "success" else None
    if not user_row:
        return ("I couldn't find a Loamy account linked to this number yet. "
                "Open the Loamy web app, go to your Dashboard, and use "
                "\"Connect WhatsApp\" to link this number - then resend your receipt.")
    user_id = user_row["user_id"]

    result = await asyncio.to_thread(process_receipt_image, file_data, mime_type, user_id)

    if not result or result.get("error"):
        return "I couldn't read that receipt clearly. Please try a sharper, well-lit photo."

    analysis = result.get("analysis", {}) or {}
    vendor = analysis.get("vendor") or "the vendor"
    cur = result.get("currency_info", {}) or {}
    ngn = cur.get("displayed_amount")

    category = categorizer.coerce_category(analysis.get("category"))

    try:
        amount_str = format_logged_amount(ngn, cur.get("original_amount"), cur.get("original_currency"))
    except (TypeError, ValueError):
        amount_str = "the amount"
    if cur.get("is_estimate"):
        amount_str += " _(est.)_"

    footer = (
        "_Tap Review Queue in Loamy to pick a category_"
        if category == categorizer.UNCATEGORIZED
        else "_Synced to your Loamy dashboard_"
    )
    return (
        "\U0001f9fe *Receipt Logged!*\n"
        f"• *Merchant*: {vendor}\n"
        f"• *Amount*: {amount_str}\n"
        f"• *Category*: {category}\n"
        "\n"
        f"{footer}"
    )


set_receipt_handler(_whatsapp_receipt_handler)



