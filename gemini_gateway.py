"""
gemini_gateway.py
Single choke point for background Gemini calls.

Every call made through generate() is:
  - admitted against a global daily cap, a per-feature hourly cap and a per-user
    hourly cap, so a bug upstream can never turn into an unbounded bill;
  - stopped by a circuit breaker after repeated failures or a quota error, so a
    broken network or an exhausted quota is not hammered every few seconds;
  - retried at most max_attempts times with exponential backoff and jitter, and
    only for transient errors;
  - sent with "thinking" disabled and a hard output-token cap, because thinking
    tokens are billed as output and these tasks (labelling, extraction) do not need them;
  - logged with token counts, so the cost of each feature is visible.

Counters live in process memory. With N workers the effective ceiling is N times the
limits below, so treat this as the safety net and keep a Google Cloud API quota as the
hard stop.
"""

import os
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

GLOBAL_DAILY_LIMIT = int(os.getenv("GEMINI_GLOBAL_DAILY_LIMIT", "1500"))
DEFAULT_FEATURE_HOURLY_LIMIT = int(os.getenv("GEMINI_FEATURE_HOURLY_LIMIT", "120"))
USER_HOURLY_LIMIT = int(os.getenv("GEMINI_USER_HOURLY_LIMIT", "60"))
FEATURE_HOURLY_LIMITS = {
    "categorize": 60,
    "onboarding_banks": 20,
    "email_extract": 120,
    "interpret_note": 120,
}

FAILURE_THRESHOLD = 5
BREAKER_OPEN_SECONDS = 10 * 60
QUOTA_OPEN_SECONDS = 15 * 60

HOUR = 3600
DAY = 86400


class GeminiGatewayError(Exception):
    """Base class: callers catch this to fall back to a non-AI path."""


class GatewayBlocked(GeminiGatewayError):
    """The call was refused locally (limit hit, breaker open, no key). No API request was made."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GeminiCallFailed(GeminiGatewayError):
    """The API was called and failed after the allowed attempts."""


_lock = threading.Lock()
_global_calls = deque()
_feature_calls = defaultdict(deque)
_user_calls = defaultdict(deque)
_consecutive_failures = 0
_open_until = 0.0
_blocked_counts = defaultdict(int)
_token_totals = defaultdict(lambda: {"calls": 0, "prompt": 0, "output": 0, "thoughts": 0})
_last_block_log = {}

_client = None
_client_lock = threading.Lock()


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


def _admit(feature: str, user_id) -> None:
    """Reserve one call or raise GatewayBlocked. Reservation happens before the request,
    so a crash mid-call still counts."""
    now = time.time()
    with _lock:
        if now < _open_until:
            _blocked_counts["circuit_open"] += 1
            _log_block("circuit_open", f"circuit open for another {int(_open_until - now)}s")
            raise GatewayBlocked("circuit_open")

        _prune(_global_calls, DAY, now)
        if len(_global_calls) >= GLOBAL_DAILY_LIMIT:
            _blocked_counts["global_daily_limit"] += 1
            _log_block("global_daily_limit", f"{len(_global_calls)} calls in 24h")
            raise GatewayBlocked("global_daily_limit")

        feature_calls = _feature_calls[feature]
        _prune(feature_calls, HOUR, now)
        feature_limit = FEATURE_HOURLY_LIMITS.get(feature, DEFAULT_FEATURE_HOURLY_LIMIT)
        if len(feature_calls) >= feature_limit:
            _blocked_counts[f"feature_hourly_limit:{feature}"] += 1
            _log_block(f"feature_hourly_limit:{feature}", f"{len(feature_calls)} calls this hour")
            raise GatewayBlocked("feature_hourly_limit")

        user_calls = None
        if user_id:
            user_calls = _user_calls[(feature, str(user_id))]
            _prune(user_calls, HOUR, now)
            if len(user_calls) >= USER_HOURLY_LIMIT:
                _blocked_counts["user_hourly_limit"] += 1
                _log_block("user_hourly_limit", f"{feature} for user {user_id}")
                raise GatewayBlocked("user_hourly_limit")

        _global_calls.append(now)
        feature_calls.append(now)
        if user_calls is not None:
            user_calls.append(now)


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


def _record_success(feature: str, response) -> None:
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


def generate(
    feature: str,
    prompt: str,
    *,
    user_id=None,
    json_mode: bool = False,
    temperature: float = 0.0,
    max_output_tokens: int = 2048,
    max_attempts: int = 2,
    model: str = DEFAULT_MODEL,
) -> str:
    """Run one guarded Gemini request and return its text ("" when the model returns none).

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
    )

    last_error = None
    for attempt in range(max(1, max_attempts)):
        _admit(feature, user_id)
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
        except Exception as e:
            last_error = e
            quota = _is_quota_error(e)
            _record_failure(feature, quota)
            print(f"[gemini] call failed feature={feature} attempt={attempt + 1}: {e}")
            if quota or attempt + 1 >= max_attempts or not _is_transient(e):
                break
            time.sleep(min(4.0, 0.8 * (2 ** attempt)) + random.uniform(0, 0.4))
            continue
        _record_success(feature, response)
        return response.text or ""

    raise GeminiCallFailed(str(last_error))


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
        return {
            "configured": is_configured(),
            "model": DEFAULT_MODEL,
            "calls_last_24h": len(_global_calls),
            "global_daily_limit": GLOBAL_DAILY_LIMIT,
            "circuit_open": now < _open_until,
            "circuit_seconds_remaining": max(0, int(_open_until - now)),
            "blocked": dict(_blocked_counts),
            "features": features,
        }
