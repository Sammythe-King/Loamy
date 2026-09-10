"""
One-off migration: local ChromaDB  ->  Supabase (Loamy Phase 1).

WHAT IT DOES
------------
Reads the local Chroma vault and copies ONE user's data (default:
iyallasamuel43@gmail.com) into the Supabase tables created for Phase 1:
users, transactions, goals, expenses, accounts, chats.

WHY IT RUNS LOCALLY
-------------------
Your Chroma data lives in a file on THIS machine (fintech_ai_vault_hidden),
so the migration must run here. It writes to Supabase over the network using
the same database.py layer the app uses, so all DB logic stays in one place
(Atomic Modularity rule).

SAFETY
------
- Idempotent: every write is an upsert keyed on the original Chroma id, so you
  can run it as many times as you like without creating duplicates.
- Read-only on Chroma: it never deletes or edits your original vault.
- Defensive: a bad row is logged and skipped, never crashes the whole run.

USAGE
-----
    set -a && source .env && set +a          # load SUPABASE_* into the env
    python scripts/migrate_chroma_to_supabase.py
    python scripts/migrate_chroma_to_supabase.py someone@else.com   # another user
"""

import os
import sys
import json

import chromadb

# Import the shared data-access layer (the ONLY module that writes to Supabase).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database  # noqa: E402


# --- locate the Chroma vault exactly like main.py does ------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "fintech_ai_vault_hidden")
TARGET_EMAIL = (sys.argv[1] if len(sys.argv) > 1 else "iyallasamuel43@gmail.com").lower().strip()


def _rows(collection):
    """Yield (id, document, metadata) for every record in a collection."""
    data = collection.get()
    ids = data.get("ids", []) or []
    docs = data.get("documents", []) or []
    metas = data.get("metadatas", []) or []
    for i in range(len(ids)):
        yield ids[i], (docs[i] if i < len(docs) else ""), (metas[i] if i < len(metas) else {}) or {}


def main():
    print(f"[migrate] Chroma path : {os.path.abspath(DB_PATH)}")
    print(f"[migrate] Target user : {TARGET_EMAIL}\n")

    if not os.path.isdir(DB_PATH):
        print(f"[migrate] ERROR: Chroma folder not found at {DB_PATH}. "
              f"Run this from the project root so the relative path resolves.")
        sys.exit(1)

    client = chromadb.PersistentClient(path=DB_PATH)

    def col(name):
        return client.get_or_create_collection(name=name)

    # 1) --- find the target user in the Chroma `users` collection ------------
    # chroma_user_id is what the LOCAL vault used to tag this user's rows; we
    # filter Chroma records by it. target_user_id is the id we actually WRITE
    # to Supabase with (resolved in step 2) -- they may differ.
    chroma_user_id = None
    user_meta = {}
    for cid, _doc, meta in _rows(col("users")):
        if (meta.get("email") or "").lower().strip() == TARGET_EMAIL:
            chroma_user_id = meta.get("user_id") or cid
            user_meta = meta
            break

    if not chroma_user_id:
        print(f"[migrate] ERROR: no user with email {TARGET_EMAIL} in Chroma `users`.")
        sys.exit(1)

    print(f"[migrate] Chroma user_id = {chroma_user_id}")

    # 2) --- reconcile against Supabase BEFORE inserting any child rows -------
    # If this email already exists in Supabase (possibly under a DIFFERENT id),
    # reuse that real id so foreign keys on transactions/goals/etc. resolve.
    # Otherwise create the user fresh using the Chroma id.
    existing = database.get_user_by_email(TARGET_EMAIL)
    if existing["status"] == "success" and existing["data"]:
        target_user_id = existing["data"]["user_id"]
        print(f"[migrate] users: already in Supabase -> reusing user_id = {target_user_id}")
        if target_user_id != chroma_user_id:
            print(f"[migrate]        (Chroma id {chroma_user_id} differs; "
                  f"all records will be attached to the Supabase id.)")
    else:
        res = database.create_user(
            user_id=chroma_user_id,
            email=user_meta.get("email", TARGET_EMAIL),
            password_hash=user_meta.get("password_hash", ""),
            full_name=user_meta.get("name"),
            business_type=user_meta.get("business_type"),
            country=user_meta.get("country"),
            selected_bank=user_meta.get("selected_bank"),
            sender_domains=user_meta.get("sender_domains", []),
            status=user_meta.get("status", "active"),
            phone_number=user_meta.get("phone_number"),
            auth_provider=user_meta.get("auth_provider"),
            created_at_original=user_meta.get("created_at"),
        )
        if res["status"] != "success":
            print(f"[migrate] ERROR creating user: {res['error']}. Aborting so we "
                  f"never attach records to a missing user_id.")
            sys.exit(1)
        target_user_id = chroma_user_id
        print(f"[migrate] users: 1 created with user_id = {target_user_id}")

    counts = {"transactions": 0, "goals": 0, "expenses": 0, "accounts": 0, "chats": 0}
    skipped = {k: 0 for k in counts}

    def belongs(meta):
        # Scope by the CHROMA user_id (that's how local rows are tagged); older
        # rows may be unscoped ('default') -> treat as the target user's.
        owner = meta.get("user_id", "default")
        return owner == chroma_user_id or owner == "default"

    # 3) --- transactions ------------------------------------------------------
    for cid, doc, meta in _rows(col("user_transactions")):
        if not belongs(meta):
            continue
        try:
            r = database.add_transaction(
                user_id=target_user_id, tx_id=cid,
                amount=meta.get("amount", 0),
                vendor=meta.get("vendor"),
                original_amount=meta.get("original_amount"),
                original_currency=meta.get("original_currency"),
                currency=meta.get("currency", "NGN"),
                date=meta.get("date"),
                category=meta.get("category"),
                transaction_type=meta.get("transaction_type"),
                source_id=meta.get("source_id"),
                is_estimate=bool(meta.get("is_estimate", False)),
                document=doc,
            )
            counts["transactions"] += 1 if r["status"] == "success" else 0
            skipped["transactions"] += 0 if r["status"] == "success" else 1
        except Exception as e:
            print(f"  [tx {cid}] skipped: {e}")
            skipped["transactions"] += 1

    # 4) --- goals -------------------------------------------------------------
    for cid, doc, meta in _rows(col("user_goals")):
        if not belongs(meta):
            continue
        try:
            r = database.add_goal(
                user_id=target_user_id, goal_id=cid,
                item=meta.get("item"),
                amount=meta.get("amount", 0),
                assigned=meta.get("assigned", 0),
                deadline=meta.get("deadline"),
                category=meta.get("category", "goal"),
                document=doc,
            )
            counts["goals"] += 1 if r["status"] == "success" else 0
            skipped["goals"] += 0 if r["status"] == "success" else 1
        except Exception as e:
            print(f"  [goal {cid}] skipped: {e}")
            skipped["goals"] += 1

    # 5) --- expenses ----------------------------------------------------------
    for cid, doc, meta in _rows(col("user_expenses")):
        if not belongs(meta):
            continue
        try:
            r = database.add_expense(
                user_id=target_user_id, expense_id=cid,
                amount=meta.get("amount", 0),
                category=meta.get("category"),
                date=meta.get("date"),
                description=meta.get("description"),
                document=doc,
            )
            counts["expenses"] += 1 if r["status"] == "success" else 0
            skipped["expenses"] += 0 if r["status"] == "success" else 1
        except Exception as e:
            print(f"  [expense {cid}] skipped: {e}")
            skipped["expenses"] += 1

    # 6) --- accounts ----------------------------------------------------------
    for cid, doc, meta in _rows(col("user_accounts")):
        if not belongs(meta):
            continue
        try:
            r = database.add_account(
                user_id=target_user_id, account_id=cid,
                account_name=meta.get("account_name"),
                balance=meta.get("balance", 0),
                currency=meta.get("currency", "NGN"),
                document=doc,
            )
            counts["accounts"] += 1 if r["status"] == "success" else 0
            skipped["accounts"] += 0 if r["status"] == "success" else 1
        except Exception as e:
            print(f"  [account {cid}] skipped: {e}")
            skipped["accounts"] += 1

    # 7) --- chats -------------------------------------------------------------
    # A Chroma chat stored the whole conversation as a JSON document, e.g.
    # {"messages": [...], "created_at": "..."}. The new Supabase `chats` table
    # is thread-shaped (one row = title + messages array), so we parse that JSON
    # back into a messages list and hand it to the thread-model add_chat().
    for cid, doc, meta in _rows(col("user_chats")):
        if not belongs(meta):
            continue
        try:
            try:
                content = json.loads(doc) if doc else {}
            except (ValueError, TypeError):
                content = {}
            messages = content.get("messages", []) if isinstance(content, dict) else []
            r = database.add_chat(
                user_id=target_user_id, chat_id=cid,
                title=meta.get("title", "Imported Chat"),
                messages=messages,
            )
            counts["chats"] += 1 if r["status"] == "success" else 0
            skipped["chats"] += 0 if r["status"] == "success" else 1
        except Exception as e:
            print(f"  [chat {cid}] skipped: {e}")
            skipped["chats"] += 1

    # 8) --- summary -----------------------------------------------------------
    print("\n[migrate] ===== DONE =====")
    for k in counts:
        print(f"  {k:<13}: {counts[k]} migrated, {skipped[k]} skipped")
    print("\n[migrate] Verify in Supabase, then test login on the web app.")


if __name__ == "__main__":
    main()
