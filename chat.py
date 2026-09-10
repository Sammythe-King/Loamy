"""
Chat History Management - Endpoints for saving and retrieving chat conversations
"""

from fastapi import APIRouter, HTTPException
import chromadb
import os
import json
import secrets
from datetime import datetime

import database  # Supabase data-access layer (now the source of truth for chats)

# Create router for chat endpoints
router = APIRouter()

# ChromaDB is kept ONLY for the legacy one-time migration endpoints below
# (migrate-history reads old goals from the vault). All live chat CRUD now
# reads/writes Supabase via database.py.
db_path = os.path.join(os.path.dirname(__file__), "..", "fintech_ai_vault_hidden")
client = chromadb.PersistentClient(path=db_path)
chats_collection = client.get_or_create_collection(name="user_chats")
transactions_collection = client.get_or_create_collection(name="user_transactions")
goals_collection = client.get_or_create_collection(name="user_goals")


def generate_chat_title(first_message):
    """
    Generate a short, clear title (max 5 words) based on the user's first message.
    Extracts the SPECIFIC item/subject when possible.
    """
    import re
    
    text = first_message.strip()
    text_lower = text.lower()
    
    # If the message is very short or just a confirmation, return generic title
    if len(text) < 5 or text_lower in ['yes', 'no', 'ok', 'okay', 'sure']:
        return 'Financial Consultation'
    
    # Try to extract specific item for goals/savings
    # Pattern: "save for [item]", "goal for [item]", "want [item]", "buy [item]", "get [item]"
    item_patterns = [
        r'(?:save|saving|goal)\s+(?:for|to\s+(?:buy|get))?\s*(?:a|an|the)?\s*(.+?)(?:\s+by|\s+until|\s+for\s+\$|\?|$)',
        r'(?:want|need|buy|get|purchase)\s+(?:a|an|the)?\s*(.+?)(?:\s+by|\s+for\s+\$|\?|$)',
        r'set\s+(?:a\s+)?goal\s+(?:for|to)?\s*(?:a|an|the)?\s*(.+?)(?:\s+by|\?|$)',
        r'(?:i\'?d?\s+like|planning)\s+(?:to\s+)?(?:save|buy|get)\s+(?:a|an|the)?\s*(.+?)(?:\s+by|\?|$)',
    ]
    
    for pattern in item_patterns:
        match = re.search(pattern, text_lower)
        if match:
            item = match.group(1).strip()
            # Clean up the item name
            item = re.sub(r'\s+', ' ', item)  # Remove extra spaces
            # Remove trailing words that aren't part of item
            item = re.sub(r'\s+(cost|costs|is|it|that|which).*$', '', item)
            if item and len(item) > 2 and len(item) < 50:
                # Title case the item
                item_title = ' '.join(w.capitalize() for w in item.split()[:4])
                return f'{item_title} Savings'
    
    # Fallback to category-based titles (but only if no item extracted)
    if any(w in text_lower for w in ['how much', 'balance', 'account', 'have in']):
        return 'Account Balance Check'
    
    if any(w in text_lower for w in ['assign', 'allocate', 'distribute']):
        return 'Fund Allocation Request'
    
    if any(w in text_lower for w in ['spend', 'spending', 'expense']):
        return 'Spending Analysis'
    
    if any(w in text_lower for w in ['budget', 'budgeting']):
        return 'Budget Planning'
    
    if any(w in text_lower for w in ['receipt', 'scan', 'purchase']):
        return 'Receipt Analysis'
    
    if any(w in text_lower for w in ['afford', 'can i buy']):
        return 'Affordability Check'
    
    if any(w in text_lower for w in ['bill', 'bills', 'payment']):
        return 'Bill Management'
    
    # Generic savings fallback only if nothing else matched
    if any(w in text_lower for w in ['save', 'saving', 'goal']):
        return 'Savings Goal Planning'
    
    return 'Financial Consultation'


def generate_receipt_title(vendor, category):
    """Generate a personalized title for receipt uploads"""
    vendor_clean = vendor.strip()
    
    # Common vendor name mappings for better titles
    vendor_titles = {
        'kfc': 'KFC Dining Expense',
        'starbucks': 'Starbucks Coffee Run',
        'mcdonalds': 'McDonald\'s Purchase',
        'walmart': 'Walmart Shopping Trip',
        'amazon': 'Amazon Order',
        'uber': 'Uber Ride Expense',
        'lyft': 'Lyft Ride Expense',
        'target': 'Target Shopping Trip',
        'costco': 'Costco Bulk Purchase',
        'whole foods': 'Whole Foods Groceries',
    }
    
    vendor_lower = vendor_clean.lower()
    for key, title in vendor_titles.items():
        if key in vendor_lower:
            return title
    
    # Default format based on category
    category_formats = {
        'food': f'{vendor_clean} Food Expense',
        'dining': f'{vendor_clean} Dining Out',
        'restaurant': f'{vendor_clean} Restaurant Visit',
        'grocery': f'{vendor_clean} Groceries',
        'shopping': f'{vendor_clean} Shopping',
        'transport': f'{vendor_clean} Transportation',
        'entertainment': f'{vendor_clean} Entertainment',
    }
    
    cat_lower = (category or 'other').lower()
    for key, fmt in category_formats.items():
        if key in cat_lower:
            return fmt
    
    return f'{vendor_clean} Purchase'


def generate_goal_title(item, amount):
    """Generate a personalized title for savings goals"""
    item_clean = item.strip().lower()
    
    # Common goal name mappings
    goal_titles = {
        'phone': 'New Phone Savings',
        'laptop': 'Laptop Fund',
        'car': 'Car Savings Plan',
        'vacation': 'Vacation Fund',
        'sneakers': 'Sneaker Fund',
        'shoes': 'New Shoes Savings',
        'computer': 'Computer Fund',
        'iphone': 'iPhone Savings',
        'macbook': 'MacBook Fund',
        'ps5': 'PS5 Gaming Fund',
        'xbox': 'Xbox Gaming Fund',
        'camera': 'Camera Savings',
        'watch': 'Watch Purchase Fund',
        'bike': 'Bike Savings Plan',
        'guitar': 'Guitar Fund',
        'headphones': 'Headphones Fund',
    }
    
    for key, title in goal_titles.items():
        if key in item_clean:
            return title
    
    # Capitalize first letter of each word (max 3 words)
    words = item.strip().split()[:3]
    item_title = ' '.join(w.capitalize() for w in words)
    
    return f'{item_title} Savings'


@router.get("/get-chats")
async def get_chats(user_id: str = "default"):
    """Get a user's saved chat conversations from Supabase (sidebar list)."""
    try:
        res = database.get_chats(user_id)
        if res["status"] != "success":
            return {"chats": [], "error": res["error"]}

        chats = []
        for row in res["data"]:
            messages = row.get("messages") or []
            chats.append({
                "id": row["id"],
                "title": row.get("title", "Untitled Chat"),
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", ""),
                "message_count": len(messages),
            })
        # database.get_chats already orders by updated_at desc.
        return {"chats": chats}
    except Exception as e:
        return {"chats": [], "error": str(e)}


@router.post("/reset-migration")
async def reset_migration():
    """Clear only auto-migrated chats (not user-created ones) for re-migration"""
    try:
        results = chats_collection.get()
        deleted = 0
        for i in range(len(results['ids'])):
            chat_id = results['ids'][i]
            
            # ONLY delete auto-migrated chats (they have special ID prefixes)
            # Do NOT delete user-created chats (even with generic titles)
            if chat_id.startswith('chat_migrated_') or chat_id.startswith('chat_goal_'):
                chats_collection.delete(ids=[chat_id])
                deleted += 1
        return {"status": "success", "deleted": deleted}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.post("/fix-chat-titles")
async def fix_chat_titles():
    """Rename chats with bad/generic titles to better rule-based titles"""
    import json
    import re
    
    try:
        results = chats_collection.get()
        fixed = 0
        
        # Bad title patterns that need fixing
        bad_patterns = [
            'scanning:', 'receipt:', 'much have', 'much money', 'assign 650', 
            'assign 1500', 'crently', 'currenly', 'analyzing receipt'
        ]
        
        for i in range(len(results['ids'])):
            chat_id = results['ids'][i]
            meta = results['metadatas'][i]
            title = meta.get('title', '')
            title_lower = title.lower()
            
            # Check if title needs fixing
            needs_fix = any(pattern in title_lower for pattern in bad_patterns)
            
            # Also check for titles that are just truncated user input with typos
            if not needs_fix:
                # Check if title looks like raw user input (has common typos or starts with verbs)
                needs_fix = (
                    title_lower.startswith('how ') or
                    title_lower.startswith('what ') or
                    title_lower.startswith('can ') or
                    'crently' in title_lower or
                    'currenly' in title_lower or
                    title_lower.startswith('much ')
                )
            
            if not needs_fix:
                continue
            
            # Get messages to generate better title
            doc = results['documents'][i] if results['documents'] else None
            if not doc:
                continue
                
            try:
                messages = json.loads(doc)
            except:
                continue
            
            # Get the first user message for context
            first_user_msg = ''
            for msg in messages:
                if msg.get('sender') == 'user':
                    first_user_msg = msg.get('content', '')
                    break
            
            if first_user_msg:
                new_title = generate_chat_title(first_user_msg)
            else:
                new_title = 'Financial Consultation'
            
            # Update the chat title
            now = datetime.now().isoformat()
            try:
                chats_collection.update(
                    ids=[chat_id],
                    metadatas=[{
                        "title": new_title,
                        "created_at": meta.get('created_at', now),
                        "updated_at": meta.get('updated_at', now),
                        "message_count": meta.get('message_count', 0)
                    }]
                )
                fixed += 1
                print(f"Fixed title: '{title}' -> '{new_title}'")
            except Exception as update_err:
                print(f"Failed to update {chat_id}: {update_err}")
        
        return {"status": "success", "fixed": fixed}
    except Exception as e:
        print(f"Fix titles error: {str(e)}")
        return {"status": "error", "error": str(e)}


@router.post("/migrate-history")
async def migrate_history():
    """Migrate existing transactions and goals to chat history"""
    import json
    migrated = 0
    
    try:
        # Get existing chats to avoid duplicates
        existing = chats_collection.get()
        existing_titles = [m.get('title', '').lower() for m in existing['metadatas']] if existing['metadatas'] else []
        
        # Note: Receipt chats are NOT auto-migrated 
        # They are created when user uploads receipts to avoid duplicate fake chats
        
        # 2. Migrate goals as chats with detailed calculations
        try:
            # First get all transactions to reference spending history
            all_transactions = []
            try:
                trans_data = transactions_collection.get()
                for j in range(len(trans_data['ids'])):
                    all_transactions.append({
                        "vendor": trans_data['metadatas'][j].get('vendor', ''),
                        "total": trans_data['metadatas'][j].get('total', 0),
                        "category": trans_data['metadatas'][j].get('category', '')
                    })
            except:
                pass
            
            goal_results = goals_collection.get()
            for i in range(len(goal_results['ids'])):
                meta = goal_results['metadatas'][i]
                item = meta.get('item', 'Unknown')
                category = meta.get('category', 'goal')
                amount = float(meta.get('amount', 0))
                title = generate_goal_title(item, amount)
                
                # Skip ALL bills (both default and user-created)
                # Bills have category="bill" - they should NOT create chat entries
                default_bills = ["rent", "utilities", "insurance", "music", "tv streaming", "annual credit card fees"]
                is_bill = category == "bill" or item.lower() in default_bills
                
                if title.lower() in existing_titles or is_bill:
                    continue
                
                amount = float(meta.get('amount', 0))
                deadline = meta.get('deadline', 'end of month')
                
                # Calculate savings breakdown
                days_estimate = 30  # Default to 30 days
                if 'week' in deadline.lower():
                    days_estimate = 7
                elif 'month' in deadline.lower():
                    days_estimate = 30
                elif 'year' in deadline.lower():
                    days_estimate = 365
                
                daily_savings = amount / days_estimate if days_estimate > 0 else 0
                weekly_savings = daily_savings * 7
                
                # Build spending reference
                spending_note = ""
                if all_transactions:
                    total_spent = sum(t['total'] for t in all_transactions)
                    spending_examples = [f"**{t['vendor']}** (${t['total']})" for t in all_transactions[:3] if t['vendor']]
                    if spending_examples:
                        spending_note = f"\n\n### Looking at Your Recent Spending\n\nI noticed you've had some recent expenses like {', '.join(spending_examples)}. Your total recent spending is **${total_spent:,.2f}**. Consider reviewing these purchases - small cutbacks in discretionary spending can add up quickly toward your {item} goal."
                
                # Create detailed AI response
                ai_response = f"""That's an exciting goal! Let me help you create a solid savings plan for your **{item}**.

## Savings Goal Analysis

- **Goal:** {item}
- **Target Amount:** ${amount:,.2f}
- **Deadline:** {deadline}

### The Math

To save **${amount:,.2f}** by {deadline}, here's what you need to set aside:

- **Daily Savings Needed:** ${daily_savings:,.2f} per day
- **Weekly Savings Needed:** ${weekly_savings:,.2f} per week

### Is This Realistic?

Saving ${daily_savings:,.2f} daily requires discipline, but it's achievable! Here are my recommendations:

1. **Prioritize essentials first** - Make sure your bills (rent, utilities) are covered before allocating to this goal
2. **Cut back on dining out** - Restaurant meals can easily be replaced with home-cooked options
3. **Review subscriptions** - Cancel any services you're not actively using
4. **Use the 24-hour rule** - Wait a day before any non-essential purchase{spending_note}

### Next Steps

I've added this goal to your Savings Goals dashboard. You can:
- **Assign funds** from your Ready to Assign balance anytime
- **Track progress** with the visual progress indicator
- **Adjust the target** if your circumstances change

Would you like me to create a detailed weekly savings schedule, or help you identify specific expenses to cut back on?"""
                
                # Create chat entry
                chat_id = f"chat_goal_{goal_results['ids'][i]}"
                now = datetime.now().isoformat()
                
                messages = [
                    {"sender": "user", "content": f"I want to save ${amount:,.0f} for {item} by {deadline}. How much do I need to save and what should I cut back on?", "isHTML": False},
                    {"sender": "ai", "content": ai_response, "isHTML": False}
                ]
                
                chats_collection.add(
                    documents=[json.dumps(messages)],
                    metadatas=[{
                        "title": title,
                        "created_at": now,
                        "updated_at": now,
                        "message_count": 2
                    }],
                    ids=[chat_id]
                )
                migrated += 1
                existing_titles.append(title.lower())
        except Exception as e:
            print(f"Goal migration error: {e}")
        
        print(f"Migrated {migrated} items to chat history")
        return {"status": "success", "migrated": migrated}
    except Exception as e:
        print(f"Migration error: {str(e)}")
        return {"status": "error", "error": str(e)}


@router.post("/create-chat")
async def create_chat(data: dict):
    """Create a new chat conversation in Supabase, scoped to its owner."""
    try:
        user_id = data.get("user_id", "default")
        first_message = data.get("first_message", "New Chat")
        title = generate_chat_title(first_message)
        messages = data.get("messages", [])

        chat_id = f"chat_{secrets.token_hex(4)}"
        res = database.add_chat(user_id=user_id, chat_id=chat_id,
                                title=title, messages=messages)
        if res["status"] != "success":
            raise HTTPException(status_code=500, detail=res["error"])

        print(f"Chat Created: {title} ({user_id})")
        return {"status": "success", "id": chat_id, "title": title}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Create Chat Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/get-chat/{chat_id}")
async def get_chat(chat_id: str):
    """Get a specific chat conversation (with all messages) from Supabase."""
    try:
        res = database.get_chat(chat_id)
        if res["status"] != "success" or not res["data"]:
            raise HTTPException(status_code=404, detail="Chat not found")

        row = res["data"]
        return {
            "id": chat_id,
            "title": row.get("title", "Untitled Chat"),
            "created_at": row.get("created_at", ""),
            "updated_at": row.get("updated_at", ""),
            "messages": row.get("messages") or [],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/update-chat/{chat_id}")
async def update_chat(chat_id: str, data: dict):
    """Replace a chat's messages (the frontend sends the FULL array)."""
    try:
        # Frontend passes the complete conversation, so we REPLACE rather than
        # append (appending would duplicate every turn).
        messages = data.get("messages", [])

        # Optionally re-derive the title from the first user message.
        new_title = None
        if data.get("update_title") and messages:
            first = messages[0].get("content", "") if isinstance(messages[0], dict) else ""
            if first:
                new_title = generate_chat_title(first)

        res = database.update_chat(chat_id, messages=messages, title=new_title)
        if res["status"] != "success":
            raise HTTPException(status_code=500, detail=res["error"])

        return {"status": "success", "title": new_title}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Update Chat Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete-chat/{chat_id}")
async def delete_chat(chat_id: str):
    """Delete a chat conversation from Supabase."""
    try:
        res = database.delete_chat(chat_id)
        if res["status"] != "success":
            raise HTTPException(status_code=500, detail=res["error"])
        print(f"Chat Deleted: {chat_id}")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Delete Chat Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/rename-chat/{chat_id}")
async def rename_chat(chat_id: str, data: dict):
    """Rename a chat conversation in Supabase."""
    try:
        new_title = data.get("title", "").strip()
        if not new_title:
            raise HTTPException(status_code=400, detail="Title cannot be empty")

        res = database.update_chat(chat_id, title=new_title)
        if res["status"] != "success":
            raise HTTPException(status_code=500, detail=res["error"])

        return {"status": "success", "title": new_title}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Rename Chat Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
