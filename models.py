"""
models.py

Shared infrastructure used across the app: environment loading, the Gemini
model client, Google OAuth client config, and the pgvector-backed collection
singletons (via vector_store.get_collection) that every data-access path
reads/writes through.

Note: the original main.py never used Pydantic request/response models -
every endpoint accepted a raw `dict` body - so there are no Pydantic schemas
to migrate. This module instead centralizes the equivalent cross-cutting
initialization that used to live scattered across main.py's module scope.
"""
import os
import google.generativeai as genai
from dotenv import load_dotenv

# Load .env before any os.getenv() calls below (e.g. the Gemini API key).
load_dotenv()

# Vector storage now lives in Supabase pgvector (see vector_store.py and
# scripts/sql/004_vector_embeddings.sql). Each name below is a Chroma-compatible
# collection object, so every existing .get/.add/.update/.delete/.query call
# works unchanged - but nothing is loaded into this process's RAM.
from vector_store import get_collection as _get_collection, health as _vector_store_health


collection = _get_collection("user_transactions")

goals_collection = _get_collection("user_goals")
accounts_collection = _get_collection("user_accounts")
expenses_collection = _get_collection("user_expenses")
chats_collection = _get_collection("user_chats") # Added to retrieve history
users_collection = _get_collection("users") # For multi-user authentication
gmail_data_collection = _get_collection("gmail_data")  # For Gmail email data
pending_conversions_collection = _get_collection("pending_conversions")  # For tracking currency estimates awaiting bank true-up
invoices_collection = _get_collection("invoices")  # For tracking money owed (invoices)
review_queue_collection = _get_collection("review_queue")  # Unrecognized bank transactions awaiting user clarification
vendor_memory_collection = _get_collection("vendor_memory")  # Learned vendor -> category rules (Smart Memory)
notifications_collection = _get_collection("notifications")  # In-app notifications (e.g. daily 5pm invoice reminder)
sync_state_collection = _get_collection("sync_state")  # Delta-sync bookmarks: last_synced_timestamp per user
gmail_credentials_collection = _get_collection("gmail_credentials")  # Per-user Gmail refresh tokens for server-side auto-sync



API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("Missing GEMINI_API_KEY. Add it to your .env file.")
genai.configure(api_key=API_KEY)

# Using gemini-2.5-flash for faster responses with good context
model = genai.GenerativeModel('gemini-2.5-flash')


# ==========================================
# GOOGLE OAUTH & GMAIL API ENDPOINTS
# ==========================================

# Google OAuth credentials — loaded from the environment, never hardcoded.
# Put the real values in your local .env (which is gitignored) so they never
# reach version control (Infrastructure Independence rule).
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
    print("[Google OAuth] WARNING: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. "
          "Add them to your .env for Google Sign-In and Gmail sync to work.")


# ==========================================
# BANK CONNECTION SCHEMAS
# ==========================================
from pydantic import BaseModel, Field


class ConnectBankRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    bank_slug: str = Field(..., min_length=1, max_length=32)
    account_tail: str = Field("", max_length=4, pattern=r"^[0-9]{0,4}$")
    nickname: str = Field("", max_length=80)


