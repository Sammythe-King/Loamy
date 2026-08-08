"""
WhatsApp Cloud API integration for Loamy.

SINGLE RESPONSIBILITY (Atomic Modularity rule): this module is ONLY the
transport bridge between Meta's WhatsApp Cloud API and Loamy's AI advisor. It
knows nothing about *how* the AI thinks - it receives messages, hands them to an
injected AI handler, and sends the reply back. Swapping the AI brain (or the
messaging channel) never requires touching the other side.

Wiring lives in main.py:
    from whatsapp import router as whatsapp_router, set_ai_handler
    app.include_router(whatsapp_router)
    set_ai_handler(<async fn(text, user_id) -> str>)
"""

import os
import asyncio
import requests
from fastapi import APIRouter, Request, Response, Query, BackgroundTasks

# Load .env if python-dotenv is available. Kept optional so a missing package
# never crashes the server (Defensive Startup Coding rule).
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

router = APIRouter()

# --- Config: never hardcode secrets (Infrastructure Independence rule) --------
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "LOAMY_SECRET_TOKEN")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_API_VERSION = "v21.0"

# --- Dependency injection for the AI "brain" ----------------------------------
# main.py plugs the advisor in via set_ai_handler(). We deliberately do NOT
# import main.py here, which keeps the two modules decoupled and avoids a
# circular import.
_ai_handler = None


def set_ai_handler(handler):
    """Register the async AI callable: handler(text: str, user_id: str) -> str."""
    global _ai_handler
    _ai_handler = handler


# ============================================
# 1. Outbound message helper
# ============================================
async def send_whatsapp_message(to_phone: str, text: str):
    """Send a plain-text WhatsApp message back to the user.

    Returns the standard Loamy contract: {status, data, error}. The blocking
    requests call is off-loaded to a thread so the event loop stays responsive.
    """
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        print("[WhatsApp] Missing WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID; cannot send.")
        return {"status": "error", "data": None, "error": "missing_credentials"}

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }

    # Guard against the most common misconfig: a placeholder / JS-style value
    # left in .env (e.g. "process.env.WHATSAPP_ACCESS_TOKEN_2"). python-dotenv
    # stores that literally, so it would be sent to Meta and rejected. Catch it
    # early with a clear message instead of a confusing 401.
    if WHATSAPP_ACCESS_TOKEN.startswith("process.env") or "your-" in WHATSAPP_ACCESS_TOKEN:
        print("[WhatsApp] WHATSAPP_ACCESS_TOKEN looks like a placeholder, not a real token. "
              "Paste the actual token (starts with 'EAA...') into .env and restart.")
        return {"status": "error", "data": None, "error": "placeholder_token"}

    try:
        print(f"[WhatsApp] Sending reply to {to_phone}...")
        resp = await asyncio.to_thread(
            requests.post, url, headers=headers, json=payload, timeout=15
        )
        if resp.status_code >= 400:
            print(f"[WhatsApp] Send failed {resp.status_code}: {resp.text}")
            # Plain-English hints for the errors you're most likely to hit in dev.
            hints = {
                190: "Access token is invalid or expired -> regenerate it in Meta and update .env.",
                131030: "Recipient not in the allowed list -> add their number under API Setup.",
                131047: "24h customer-service window is closed -> the user must message you again first.",
                131051: "Wrong message type for this window (use a template outside 24h).",
            }
            try:
                code = resp.json().get("error", {}).get("code")
                if code in hints:
                    print(f"[WhatsApp] Hint (error {code}): {hints[code]}")
            except Exception:
                pass
            return {"status": "error", "data": None, "error": f"http_{resp.status_code}"}
        print(f"[WhatsApp] Reply delivered to {to_phone}.")
        return {"status": "success", "data": resp.json(), "error": None}
    except Exception as e:
        print(f"[WhatsApp] Send exception: {e}")
        return {"status": "error", "data": None, "error": str(e)}


# ============================================
# 2. Meta webhook verification (GET)
# ============================================
@router.get("/api/whatsapp/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Meta calls this once to confirm the webhook. Echo the challenge back."""
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        print("[WhatsApp] Webhook verified.")
        return Response(content=hub_challenge or "", media_type="text/plain", status_code=200)
    print("[WhatsApp] Webhook verification FAILED (bad mode/token).")
    return Response(content="Verification failed", status_code=403)


# ============================================
# 3. Incoming message receiver (POST)
# ============================================
@router.post("/api/whatsapp/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Acknowledge Meta immediately (prevents retries), then reply in the
    background so slow AI/network work never delays the 200 response."""
    try:
        payload = await request.json()
    except Exception:
        # Even malformed payloads must get a 200 so Meta stops retrying.
        return {"status": "success"}

    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if messages:
            message = messages[0]
            # Only handle text messages; ignore statuses, media, etc. for now.
            if message.get("type") == "text":
                from_phone = message["from"]
                body = message["text"]["body"]
                msg_id = message.get("id", "")
                print(f"[WhatsApp] Incoming from {from_phone} ({msg_id}): {body}")
                background_tasks.add_task(_process_and_reply, from_phone, body)
    except (KeyError, IndexError, TypeError) as e:
        # Status/delivery callbacks and other event shapes land here - not errors.
        print(f"[WhatsApp] Non-message webhook ignored: {e}")

    return {"status": "success"}


# ============================================
# 4. Integration flow: message -> AI -> reply
# ============================================
async def _process_and_reply(from_phone: str, body: str):
    """Hand the text to the injected AI advisor and text the answer back.

    The phone number doubles as the user_id so replies stay scoped per sender.
    """
    if _ai_handler is None:
        print("[WhatsApp] No AI handler registered; dropping message.")
        return

    try:
        reply = await _ai_handler(body, from_phone)
        reply = (reply or "").strip() or "I couldn't generate a response just now. Please try again."
        await send_whatsapp_message(from_phone, reply)
    except Exception as e:
        print(f"[WhatsApp] AI processing failed: {e}")
        await send_whatsapp_message(
            from_phone,
            "Sorry, I hit a snag processing that. Please try again in a moment.",
        )
