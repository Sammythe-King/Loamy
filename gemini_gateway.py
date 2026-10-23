"""
gemini_gateway.py
Single choke point for every Gemini generation call in the backend.

Every call made through generate() / generate_response() is:
  - admitted against a global daily cap, a per-feature hourly cap, a per-user-per-feature
    hourly cap (GEMINI_USER_HOURLY_LIMIT) and a per-user total hourly cap, so a bug
    upstream or one abusive user can never turn into an unbounded bill;
  - stopped by a circuit breaker after repeated failures or a quota error, so a broken
    network or an exhausted quota is not hammered every few seconds;
  - retried at most max_attempts times with exponential backoff and jitter, and only for
    transient errors;
  - sent with "thinking" disabled and a hard output-token cap, because thinking tokens
    are billed as output;
  - logged with token counts to Supabase, so the cost of each feature and user is visible.

Counters live in Supabase (gemini_rate_limits, via the gemini_admit() SQL function in
scripts/sql/005_gemini_rate_limits.sql), so limits survive restarts and are shared by
every instance. If Supabase cannot be reached the gateway falls back to the in-process
counters below, which are per instance but still bound the spend. The circuit breaker is
per instance by design: it only pauses calls that would fail anyway.
"""

import concurrent.futures
import os
import queue
import random
import threading
import time
from collections import defaultdict, deque

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
REQUEST_TIMEOUT_MS = 30_000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


GLOBAL_DAILY_LIMIT = _env_int("GEMINI_GLOBAL_DAILY_LIMIT", 1500)
DEFAULT_FEATURE_HOURLY_LIMIT = _env_int("GEMINI_FEATURE_HOURLY_LIMIT", 120)
USER_HOURLY_LIMIT = _env_int("GEMINI_USER_HOURLY_LIMIT", 60)
USER_TOTAL_HOURLY_LIMIT = _env_int("GEMINI_USER_TOTAL_HOURLY_LIMIT", 150)

FEATURE_HOURLY_LIMITS = {
    "categorize": 60,
    "onboarding_banks": 20,
    "email_extract": 120,
    "interpret_note": 120,
    "chat_reply": 400,
    "chat_goal": 200,
    "chat_expense": 300,
    "receipt_vision": 100,
}


def _apply_feature_limit_overrides() -> None:
    """GEMINI_FEATURE_LIMITS="chat_reply=800,categorize=100" overrides the defaults above."""
    for pair in os.getenv("GEMINI_FEATURE_LIMITS", "").split(","):
        name, _, value = pair.partition("=")
        if name.strip() and value.strip().isdigit():
            FEATURE_HOURLY_LIMITS[name.strip()] = int(value.strip())


_apply_feature_limit_overrides()

FAILURE_THRESHOLD = 5
BREAKER_OPEN_SECONDS = 10 * 60
QUOTA_OPEN_SECONDS = 15 * 60

STORE_TIMEOUT_SECONDS = 3.0
STORE_RETRY_AFTER_SECONDS = 30
USAGE_LOG_QUEUE_SIZE = 1000

HOUR = 3600
DAY = 86400

USER_FACING_MESSAGES = {
    "user_hourly_limit": "You've sent a lot of requests in the last hour. Please try again a little later.",
    "user_total_hourly_limit": "You've sent a lot of requests in the last hour. Please try again a little later.",
}
BUSY_MESSAGE = "I'm a bit overloaded right now. Please try again in a few minutes."


class GeminiGatewayError(Exception):
    """Base class: callers catch this to fall back to a non-AI path."""


class GatewayBlocked(GeminiGatewayError):
    """The call was refused locally (limit hit, breaker open, no key). No API request was made."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GeminiCallFailed(GeminiGatewayError):
    """The API was called and failed after the allowed attempts."""


def user_message(err: Exception) -> str:
    """A short, safe message to show a chat user when a call could not be answered."""
    if isinstance(err, GatewayBlocked):
        return USER_FACING_MESSAGES.get(err.reason, BUSY_MESSAGE)
    return "I'm having trouble thinking right now. Please try again in a moment."


_lock = threading.Lock()
_global_calls = deque()
_feature_calls = defaultdict(deque)
_user_calls = defaultdict(deque)
_user_total_calls = defaultdict(deque)
_consecutive_failures = 0
_open_until = 0.0
_blocked_counts = defaultdict(int)
_token_totals = defaultdict(lambda: {"calls": 0, "prompt": 0, "output": 0, "thoughts": 0})
_last_block_log = {}
_store_down_until = 0.0
_store_mode = "unknown"

_client = None
_client_lock = threading.Lock()

_store_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini-store")
_usage_queue = queue.Queue(maxsize=USAGE_LOG_QUEUE_SIZE)
_usage_worker = None
_usage_worker_lock = threading.Lock()


def _api_key():
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def is_configured() -> bool:
    return bool(_api_key())


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            key = _api_key()
            if not key:
                return None
            from google import genai
            from google.genai import types
            _client = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
            )
        return _client


def _prune(calls: deque, window: float, now: float) -> None:
    while calls and now - calls[0] > window:
        calls.popleft()


def _log_block(reason: str, detail: str) -> None:
    now = time.time()
    if now - _last_block_log.get(reason, 0) >= 60:
        _last_block_log[reason] = now
        print(f"[gemini] BLOCKED ({reason}): {detail}")


def _block(reason: str, detail: str, count_key: str = None) -> None:
    with _lock:
        _blocked_counts[count_key or reason] += 1
    _log_block(reason, detail)
    raise GatewayBlocked(reason)


# ---------------------------------------------------------------------------
# Admission: Supabase first, in-process fallback
# ---------------------------------------------------------------------------

def _supabase():
    import database
    return database.supabase


def _admit_in_store(feature: str, user_id) -> str:
    """Ask the gemini_admit() SQL function to reserve one call. Returns 'ok' or a reason.
    Raises when the store cannot be reached or does not answer within the timeout."""
    feature_limit = FEATURE_HOURLY_LIMITS.get(feature, DEFAULT_FEATURE_HOURLY_LIMIT)
    params = {
        "p_feature": feature,
        "p_user_id": str(user_id) if user_id else None,
        "p_global_daily": GLOBAL_DAILY_LIMIT,
        "p_feature_hourly": feature_limit,
        "p_user_feature_hourly": USER_HOURLY_LIMIT,
        "p_user_total_hourly": USER_TOTAL_HOURLY_LIMIT,
    }
    future = _store_pool.submit(lambda: _supabase().rpc("gemini_admit", params).execute())
    result = future.result(timeout=STORE_TIMEOUT_SECONDS)
    verdict = result.data
    if isinstance(verdict, list):
        verdict = verdict[0] if verdict else None
    if not isinstance(verdict, str):
        raise RuntimeError(f"unexpected gemini_admit response: {verdict!r}")
    return verdict


def _check_in_memory(feature: str, user_id, now: float) -> None:
    """Sliding-window check used only while Supabase is unreachable. Caller holds _lock."""
    _prune(_global_calls, DAY, now)
    if len(_global_calls) >= GLOBAL_DAILY_LIMIT:
        raise GatewayBlocked("global_daily_limit")

    feature_calls = _feature_calls[feature]
    _prune(feature_calls, HOUR, now)
    if len(feature_calls) >= FEATURE_HOURLY_LIMITS.get(feature, DEFAULT_FEATURE_HOURLY_LIMIT):
        raise GatewayBlocked("feature_hourly_limit")

    if user_id:
        user_calls = _user_calls[(feature, str(user_id))]
        _prune(user_calls, HOUR, now)
        if len(user_calls) >= USER_HOURLY_LIMIT:
            raise GatewayBlocked("user_hourly_limit")
        total_calls = _user_total_calls[str(user_id)]
        _prune(total_calls, HOUR, now)
        if len(total_calls) >= USER_TOTAL_HOURLY_LIMIT:
            raise GatewayBlocked("user_total_hourly_limit")


def _record_in_memory(feature: str, user_id, now: float) -> None:
    """Keep the in-process counters current on every admitted call, so the fallback starts
    from realistic numbers if Supabase drops out. Caller holds _lock."""
    _global_calls.append(now)
    _feature_calls[feature].append(now)
    if user_id:
        _user_calls[(feature, str(user_id))].append(now)
        _user_total_calls[str(user_id)].append(now)


def _admit(feature: str, user_id) -> None:
    """Reserve one call or raise GatewayBlocked. Reservation happens before the request,
    so a crash mid-call still counts."""
    global _store_down_until, _store_mode
    now = time.time()

    with _lock:
        circuit_remaining = _open_until - now
    if circuit_remaining > 0:
        _block("circuit_open", f"circuit open for another {int(circuit_remaining)}s")

    if now >= _store_down_until:
        try:
            verdict = _admit_in_store(feature, user_id)
        except Exception as e:
            _store_down_until = now + STORE_RETRY_AFTER_SECONDS
            _store_mode = "memory_fallback"
            print(f"[gemini] rate-limit store unavailable, using in-process counters for "
                  f"{STORE_RETRY_AFTER_SECONDS}s: {e}")
        else:
            _store_mode = "supabase"
            if verdict != "ok":
                count_key = f"feature_hourly_limit:{feature}" if verdict == "feature_hourly_limit" else verdict
                _block(verdict, f"{feature} for user {user_id}", count_key)
            with _lock:
                _record_in_memory(feature, user_id, now)
            return

    with _lock:
        try:
            _check_in_memory(feature, user_id, now)
        except GatewayBlocked as blocked:
            _blocked_counts[blocked.reason] += 1
            _log_block(blocked.reason, f"{feature} for user {user_id} (in-process counters)")
            raise
        _record_in_memory(feature, user_id, now)


# ---------------------------------------------------------------------------
# Failure handling and usage logging
# ---------------------------------------------------------------------------

def _is_quota_error(err: Exception) -> bool:
    text = str(err).lower()
    return "429" in text or "resource_exhausted" in text or "quota" in text


def _is_transient(err: Exception) -> bool:
    text = str(err).lower()
    return any(marker in text for marker in (
        "500", "502", "503", "504", "timeout", "timed out", "unavailable",
        "connection", "deadline", "temporarily",
    ))


def _record_failure(feature: str, quota: bool) -> None:
    global _consecutive_failures, _open_until
    with _lock:
        _consecutive_failures += 1
        if quota:
            _open_until = time.time() + QUOTA_OPEN_SECONDS
            print(f"[gemini] quota/rate error on '{feature}': pausing all Gemini calls for {QUOTA_OPEN_SECONDS // 60} min")
        elif _consecutive_failures >= FAILURE_THRESHOLD:
            _open_until = time.time() + BREAKER_OPEN_SECONDS
            _consecutive_failures = 0
            print(f"[gemini] {FAILURE_THRESHOLD} consecutive failures: pausing all Gemini calls for {BREAKER_OPEN_SECONDS // 60} min")


def _usage_log_loop() -> None:
    while True:
        row = _usage_queue.get()
        try:
            _supabase().table("gemini_usage_logs").insert(row).execute()
        except Exception as e:
            print(f"[gemini] usage log write failed: {e}")


def _enqueue_usage_log(row: dict) -> None:
    """Hand a usage row to a background writer so logging never adds latency or raises.
    When the queue is full the row is dropped; the counters already hold the truth."""
    global _usage_worker
    with _usage_worker_lock:
        if _usage_worker is None:
            _usage_worker = threading.Thread(target=_usage_log_loop, name="gemini-usage-log", daemon=True)
            _usage_worker.start()
    try:
        _usage_queue.put_nowait(row)
    except queue.Full:
        pass


def _record_success(feature: str, user_id, model: str, response, latency_ms: int) -> None:
    global _consecutive_failures
    usage = getattr(response, "usage_metadata", None)
    prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
    output = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
    with _lock:
        _consecutive_failures = 0
        totals = _token_totals[feature]
        totals["calls"] += 1
        totals["prompt"] += prompt
        totals["output"] += output
        totals["thoughts"] += thoughts
    print(f"[gemini] feature={feature} prompt_tokens={prompt} output_tokens={output} thought_tokens={thoughts}")
    _enqueue_usage_log({
        "feature": feature, "user_id": str(user_id) if user_id else None, "model": model,
        "status": "ok", "prompt_tokens": prompt, "output_tokens": output,
        "thought_tokens": thoughts, "latency_ms": latency_ms,
    })


def _log_failed_call(feature: str, user_id, model: str, error: Exception, latency_ms: int) -> None:
    _enqueue_usage_log({
        "feature": feature, "user_id": str(user_id) if user_id else None, "model": model,
        "status": "failed", "latency_ms": latency_ms, "error": str(error)[:500],
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _build_contents(contents):
    """Accept a plain string, or a list mixing strings and {"mime_type", "data"} dicts
    (images, PDFs), and return what the SDK expects."""
    if isinstance(contents, str):
        return contents
    from google.genai import types
    parts = []
    for item in contents:
        if isinstance(item, dict) and "mime_type" in item and "data" in item:
            parts.append(types.Part.from_bytes(data=item["data"], mime_type=item["mime_type"]))
        else:
            parts.append(item)
    return parts


def generate_response(
    feature: str,
    contents,
    *,
    user_id=None,
    json_mode: bool = False,
    temperature: float = 0.0,
    max_output_tokens: int = 2048,
    max_attempts: int = 2,
    model: str = DEFAULT_MODEL,
    tools=None,
):
    """Run one guarded Gemini request and return the SDK response object.

    `contents` is a prompt string or a list of strings and {"mime_type", "data"} dicts.
    `tools` takes function declarations as {"function_declarations": [...]} dicts.
    Raises GatewayBlocked when refused locally, GeminiCallFailed when the API failed.
    Both subclass GeminiGatewayError, so callers can catch that one type and degrade."""
    client = _get_client()
    if client is None:
        raise GatewayBlocked("missing_api_key")

    from google.genai import types

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json" if json_mode else None,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=tools,
    )
    payload = _build_contents(contents)

    last_error = None
    for attempt in range(max(1, max_attempts)):
        _admit(feature, user_id)
        started = time.time()
        try:
            response = client.models.generate_content(model=model, contents=payload, config=config)
        except Exception as e:
            last_error = e
            quota = _is_quota_error(e)
            _record_failure(feature, quota)
            _log_failed_call(feature, user_id, model, e, int((time.time() - started) * 1000))
            print(f"[gemini] call failed feature={feature} attempt={attempt + 1}: {e}")
            if quota or attempt + 1 >= max_attempts or not _is_transient(e):
                break
            time.sleep(min(4.0, 0.8 * (2 ** attempt)) + random.uniform(0, 0.4))
            continue
        _record_success(feature, user_id, model, response, int((time.time() - started) * 1000))
        return response

    raise GeminiCallFailed(str(last_error))


def generate(feature: str, prompt, **kwargs) -> str:
    """Like generate_response() but returns the text ("" when the model returns none)."""
    return generate_response(feature, prompt, **kwargs).text or ""


def function_calls(response) -> list:
    """Return [{"name": ..., "args": {...}}] for every function call in a response."""
    calls = []
    for call in getattr(response, "function_calls", None) or []:
        calls.append({"name": call.name, "args": dict(call.args or {})})
    return calls


def _store_snapshot() -> dict:
    """Current-window counters from Supabase. Best effort: returns {} if unreachable."""
    try:
        now = time.gmtime()
        hour_start = time.strftime("%Y-%m-%dT%H:00:00+00:00", now)
        day_start = time.strftime("%Y-%m-%dT00:00:00+00:00", now)
        client = _supabase()
        day = (client.table("gemini_rate_limits").select("calls")
               .eq("scope", "global_day").eq("window_start", day_start).execute())
        features = (client.table("gemini_rate_limits").select("key,calls")
                    .eq("scope", "feature_hour").eq("window_start", hour_start).execute())
        return {
            "calls_today": sum(int(r["calls"]) for r in (day.data or [])),
            "feature_calls_this_hour": {r["key"]: int(r["calls"]) for r in (features.data or [])},
        }
    except Exception as e:
        return {"error": str(e)[:200]}


def usage_snapshot() -> dict:
    """Counters for the /health/ai-usage endpoint. Contains no user data."""
    now = time.time()
    with _lock:
        _prune(_global_calls, DAY, now)
        features = {}
        for feature, calls in _feature_calls.items():
            _prune(calls, HOUR, now)
            features[feature] = {
                "calls_last_hour": len(calls),
                "hourly_limit": FEATURE_HOURLY_LIMITS.get(feature, DEFAULT_FEATURE_HOURLY_LIMIT),
                **_token_totals.get(feature, {}),
            }
        snapshot = {
            "configured": is_configured(),
            "model": DEFAULT_MODEL,
            "counter_store": _store_mode,
            "calls_last_24h_this_instance": len(_global_calls),
            "global_daily_limit": GLOBAL_DAILY_LIMIT,
            "user_hourly_limit": USER_HOURLY_LIMIT,
            "user_total_hourly_limit": USER_TOTAL_HOURLY_LIMIT,
            "circuit_open": now < _open_until,
            "circuit_seconds_remaining": max(0, int(_open_until - now)),
            "blocked": dict(_blocked_counts),
            "features": features,
        }
    snapshot["persistent"] = _store_snapshot()
    return snapshot
