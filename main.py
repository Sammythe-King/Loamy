from fastapi import FastAPI, UploadFile, File, HTTPException, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
import json
import os
import re
import asyncio
import threading
import hashlib
import secrets
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load .env before any os.getenv() calls below (e.g. the Gemini API key).
load_dotenv()


# Import chat router
from chat import router as chat_router
# Import WhatsApp transport module (Meta Cloud API webhooks + outbound sends)
from whatsapp import router as whatsapp_router
from routes import router as api_router
import business_logic
import email_parser
import chat_service

from contextlib import asynccontextmanager



@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The scheduler helpers are defined further down in this module; they
    # resolve at call time, after the module has loaded. No model warmup is
    # needed anymore: embeddings are a Gemini API call, vectors live in Supabase.
    _start_scheduler()
    try:
        yield
    finally:
        _stop_scheduler()



app = FastAPI(lifespan=lifespan)

# Include chat routes

app.include_router(chat_router)
# Include WhatsApp webhook routes
app.include_router(whatsapp_router)
app.include_router(api_router)

# NOTE on where CORSMiddleware gets registered: Starlette's add_middleware()
# inserts each call at position 0 of its internal list, then wraps the ASGI
# app in REVERSE registration order. That means the LAST middleware added ends
# up OUTERMOST (it sees every request first and every response last). The
# actual app.add_middleware(CORSMiddleware, ...) call lives further down this
# file - AFTER owner_kill_switch and vector_store_readiness_gate are defined -
# so CORS wraps both of them. That guarantees every OPTIONS preflight is
# answered with CORS headers before either custom middleware runs, and every
# response they short-circuit (e.g. the kill switch's 503) still gets
# Access-Control-Allow-Origin attached. Registering it here instead (textually
# first, as most tutorials show) would make it innermost and bring back this
# exact "no Access-Control-Allow-Origin on preflight" bug.



def _cors_headers_for(request: Request) -> dict:
    # Origin-reflecting wildcard: allow_credentials=True requires a concrete
    # origin (the CORS spec forbids a literal "*" alongside credentials), so
    # this mirrors what CORSMiddleware itself does and reflects whatever
    # Origin the caller sent. Used for responses built by hand (error/503
    # handlers) that bypass CORSMiddleware's own response-header injection.
    origin = request.headers.get("origin")
    if origin:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return {}



# Starlette routes unhandled exceptions to ServerErrorMiddleware, which sits
# OUTSIDE CORSMiddleware, so a naked 500 ships without CORS headers and the
# browser misreports it as a CORS failure. Attach the headers here explicitly.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"[Unhandled] {request.method} {request.url.path}: {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"status": "error", "data": None, "error": "internal_server_error"},
        headers=_cors_headers_for(request),
    )


# ============================================
# OWNER KILL SWITCH
# ============================================
# A single owner-controlled flag that can disable the entire backend on demand.
# Because the value lives ONLY in the environment (never in git), only whoever
# controls the deployment env can flip it. Set LOAMY_KILL_SWITCH=on to make the
# API refuse all traffic; unset it (or any other value) to run normally.
# This is single-purpose and dependency-free (Atomic Modularity rule).
from fastapi.responses import JSONResponse
from starlette.requests import Request as _StarletteRequest



def _kill_switch_active() -> bool:
    return os.getenv("LOAMY_KILL_SWITCH", "").strip().lower() in ("on", "true", "1", "yes")


@app.middleware("http")
async def owner_kill_switch(request: _StarletteRequest, call_next):
    # /health stays reachable so you can confirm the server itself is up while
    # the app logic is intentionally disabled.
    if _kill_switch_active() and request.url.path != "/health":
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "data": None,
                "error": "Service temporarily disabled by the owner.",
            },
        )
    return await call_next(request)



# ============================================
# CORS (registered last on purpose - see the NOTE near app = FastAPI() above)
# ============================================
# Everything that matters for "No 'Access-Control-Allow-Origin' header on
# OPTIONS" lives here: add_middleware() wraps in reverse-registration order, so
# adding CORSMiddleware AFTER owner_kill_switch and vector_store_readiness_gate
# makes it the outermost layer. It now intercepts every preflight OPTIONS
# request itself, before either of those middlewares runs, and it wraps their
# responses too, so every reply - success, 503, or otherwise - carries CORS
# headers. allow_origins=["*"] + allow_credentials=True is intentional:
# Starlette detects credentialed requests and reflects the caller's actual
# Origin instead of a literal "*" (the CORS spec forbids combining a literal
# wildcard with credentials), so this is both permissive and spec-compliant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)



# ============================================
# BACKGROUND SCHEDULER (daily 5pm invoice reminder)
# ============================================
# NOTE on bank-data sync: the freshest bank data can only be pulled from the
# BROWSER, because the Gmail OAuth access token lives in the client's
# localStorage (set during the Gmail connect flow). The server has no token, so
# a server-side timer cannot fetch new emails. The real activity-driven sync is
# therefore implemented on the frontend (see src/services/activitySyncService.js
# + src/hooks/useGlobalActivitySync.js). This scheduler only handles work the
# server CAN do on its own, like the daily invoice reminder below.


# --- Daily 5pm invoice reminder -----------------------------------------------
# Single responsibility: create ONE in-app notification per day asking the user
# whether they have an invoice to log. Idempotent - re-running on the same day
# will not create duplicates.
INVOICE_REMINDER_HOUR = 17  # 5 PM, 24-hour clock



def _invoice_reminder_job():
    """Wrapper the scheduler calls every day at 5 PM."""
    print("[Reminder] Running daily 5pm invoice reminder job...")
    business_logic.create_invoice_reminder()



# --- Periodic Gmail auto-sync (keeps data fresh with no browser open) --------
# root cause of "info doesn't update on its own": run_server_side_gmail_sync /
# maybe_autosync_all_users already exist below and CAN sync a user's bank data
# entirely server-side (mint an access token from their stored refresh token),
# but the only caller was GET /get-dashboard-data - i.e. sync only fired when
# someone had the WEB dashboard open. A WhatsApp-only user (laptop off, no
# dashboard tab) never triggered it, so their balance/spending on WhatsApp went
# stale. This job calls the same maybe_autosync_all_users() on a fixed interval
# instead, so every linked user's data refreshes on its own regardless of
# which channel (web or WhatsApp) they actually use. Each user is still
# individually throttled by SERVER_SYNC_COOLDOWN_SECONDS, so a short interval
# here doesn't cause extra Gmail API load.
GMAIL_AUTOSYNC_INTERVAL_MINUTES = 10



def _gmail_autosync_job():
    """Wrapper the scheduler calls every GMAIL_AUTOSYNC_INTERVAL_MINUTES."""
    print("[Scheduler] Running periodic Gmail auto-sync for all linked users...")
    try:
        # maybe_autosync_all_users is defined later in this module, but by the
        # time the scheduler actually invokes this job (after app startup) the
        # whole module has finished loading, so the name resolves fine.
        asyncio.run(email_parser.maybe_autosync_all_users(force=False))
    except Exception as e:
        print(f"[Scheduler] Gmail auto-sync job failed: {e}")



# Build the scheduler lazily so a missing apscheduler install can't crash import.
scheduler = None
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(daemon=True)
    # Daily invoice reminder at 5:00 PM local server time.
    scheduler.add_job(
        _invoice_reminder_job,
        CronTrigger(hour=INVOICE_REMINDER_HOUR, minute=0),
        id="daily_invoice_reminder",
        replace_existing=True,
    )
    # Periodic Gmail auto-sync so WhatsApp-only usage still stays up to date.
    scheduler.add_job(
        _gmail_autosync_job,
        "interval",
        minutes=GMAIL_AUTOSYNC_INTERVAL_MINUTES,
        id="periodic_gmail_autosync",
        replace_existing=True,
    )
except Exception as e:  # pragma: no cover - environment without apscheduler
    print(f"[Scheduler] APScheduler unavailable, scheduled jobs disabled: {e}")



def _start_scheduler():
    """Start the background scheduler cleanly on app startup."""
    if scheduler and not scheduler.running:
        scheduler.start()
        print(
            f"[Scheduler] Started. Daily invoice reminder set for "
            f"{INVOICE_REMINDER_HOUR}:00, Gmail auto-sync every "
            f"{GMAIL_AUTOSYNC_INTERVAL_MINUTES} min."
        )



def _stop_scheduler():
    """Shut the scheduler down cleanly on app termination."""
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        print("[Scheduler] Stopped.")




# Register the WhatsApp advisor + receipt handlers (implemented in
# chat_service.py) with the WhatsApp transport module.
from whatsapp import set_ai_handler, set_receipt_handler
set_ai_handler(chat_service._whatsapp_ai_handler)
set_receipt_handler(chat_service._whatsapp_receipt_handler)
