// activitySyncService.js
// ---------------------------------------------------------------------------
// Owns the REAL background bank-data sync that runs from the browser.
//
// Why client-side? The Gmail OAuth access token lives in localStorage (set
// during the Gmail connect flow). The FastAPI server has no token of its own,
// so it physically cannot pull fresh bank emails on a timer. The freshest data
// can only be fetched from where the token is: the browser. This service
// replicates the manual "Refresh" flow (fetch-emails -> token refresh on 401 ->
// sync-gmail-data) and runs it automatically on activity.
//
// Single responsibility: perform one sync, enforce a client-side cooldown, and
// announce success. It does NOT decide WHEN to run (that's the hook) and does
// NOT touch UI (consumers listen for the broadcast event).
// ---------------------------------------------------------------------------

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

// Keys we read/write in localStorage.
const LAST_SYNC_KEY = "loamy_last_sync_at";
const ACCESS_TOKEN_KEY = "gmail_access_token";
const REFRESH_TOKEN_KEY = "gmail_refresh_token";
const SESSION_KEY = "loamy_session";

// Minimum gap between two sync ATTEMPTS (client-side cooldown shield).
const SYNC_COOLDOWN_MS = 5 * 60 * 1000; // 5 minutes

// Event other components (e.g. the Dashboard) listen for to re-fetch their data
// once fresh bank emails have landed.
export const DATA_SYNCED_EVENT = "loamy:data-synced";

// Module-level single-flight guard. Because every consumer imports the same
// module instance, this prevents two syncs from overlapping within a tab even
// before the cooldown timestamp is written to localStorage.
let _syncInFlight = false;

const INVALID_USER_IDS = new Set(["", "default", "default_user", "null", "undefined", "none"]);

/** True only for a real logged-in user id (not a placeholder fallback). */
export function isRealUserId(userId) {
  return typeof userId === "string" && !INVALID_USER_IDS.has(userId.trim().toLowerCase());
}

const POLL_INTERVAL_MS = 3000;
const POLL_TIMEOUT_MS = 3 * 60 * 1000;

/**
 * The backend now answers /gmail/fetch-emails with {status:"processing"} and
 * does the fetch+store in the background. Poll until it finishes and return
 * the stored {status, emails, stats}.
 */
export async function waitForServerSync(userId) {
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    try {
      const res = await fetch(`${API_URL}/gmail/sync-status/${encodeURIComponent(userId)}`);
      const data = await res.json();
      if (data.status !== "processing") return data;
    } catch (err) {
      console.error("[v0] Sync: status poll failed:", err);
    }
  }
  return { status: "error", error: "sync_timeout" };
}

/** Has the cooldown window elapsed since the last sync ATTEMPT? */
function isCooldownElapsed() {
  const last = Number(localStorage.getItem(LAST_SYNC_KEY) || 0);
  if (!last) return true;
  return Date.now() - last >= SYNC_COOLDOWN_MS;
}

/**
 * Claim the cooldown slot up front (synchronously, before any await) so that
 * concurrent/rapid calls all bail. We stamp the timestamp at the START of an
 * attempt - not only on success - so that a FAILED sync (e.g. expired token)
 * cannot retrigger on every mousemove and create a request storm.
 */
function claimCooldownSlot() {
  localStorage.setItem(LAST_SYNC_KEY, String(Date.now()));
}

/** Exchange the stored refresh token for a fresh access token. */
async function refreshAccessToken() {
  const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY);
  if (!refreshToken) return null;
  try {
    const res = await fetch(`${API_URL}/gmail/refresh-token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    const data = await res.json();
    if (data.status === "success" && data.access_token) {
      localStorage.setItem(ACCESS_TOKEN_KEY, data.access_token);
      return data.access_token;
    }
  } catch (err) {
    console.error("[v0] Sync: token refresh failed:", err);
  }
  return null;
}

/**
 * Run one real bank-data sync.
 *
 * @param {Object}  opts
 * @param {boolean} opts.force       Bypass the cooldown (e.g. app just opened).
 * @param {boolean} opts.fullResync  Ignore the server bookmark and re-pull the
 *                                   full recent window (manual "Refresh Data").
 * @returns {Promise<{synced:boolean, throttled?:boolean, reason?:string, count?:number}>}
 */
export async function runActivitySync({ force = false, fullResync = false } = {}) {
  // Single-flight: never let two syncs overlap within this tab.
  if (_syncInFlight) {
    return { synced: false, throttled: true, reason: "in_flight" };
  }

  // No Gmail connection -> nothing to sync.
  const accessToken = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (!accessToken) {
    return { synced: false, reason: "no_gmail_token" };
  }

  const session = JSON.parse(localStorage.getItem(SESSION_KEY) || "{}");
  const userId = session.user_id;
  if (!isRealUserId(userId)) {
    return { synced: false, reason: "no_session" };
  }

  // Cooldown shield: skip if we attempted a sync very recently (unless forced).
  if (!force && !isCooldownElapsed()) {
    return { synced: false, throttled: true, reason: "cooldown" };
  }

  // Claim the slot BEFORE any async work so failed syncs (e.g. expired token)
  // can't retrigger on every mousemove. Forced syncs (manual refresh) also
  // claim it so a click-spam can't storm the server either.
  claimCooldownSlot();
  _syncInFlight = true;

  const doFetch = async (token) => {
    const res = await fetch(`${API_URL}/gmail/fetch-emails`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_token: token, user_id: userId, full_resync: fullResync }),
    });
    return res.json();
  };

  try {
    let data = await doFetch(accessToken);

    // Token expired? Refresh once and retry.
    const tokenExpired =
      data.requires_reauth ||
      data.error === "token_expired" ||
      (data.status === "error" &&
        (String(data.error).includes("401") ||
          String(data.error).includes("UNAUTHENTICATED")));

    if (tokenExpired) {
      const newToken = await refreshAccessToken();
      if (!newToken) {
        return { synced: false, reason: "reauth_required" };
      }
      data = await doFetch(newToken);
    }

    if (data.status === "processing") {
      data = await waitForServerSync(userId);
    }

    if (data.status !== "success") {
      return { synced: false, reason: data.error || "fetch_failed" };
    }

    // The backend already stored the emails; just cache locally and announce.
    const emailData = {
      emails: data.emails || [],
      stats: data.stats || {},
      total_scanned: data.stats?.emailsScanned || 0,
    };
    localStorage.setItem("gmail_email_data", JSON.stringify(emailData));

    // Reset the cooldown clock to completion time and announce success.
    localStorage.setItem(LAST_SYNC_KEY, String(Date.now()));
    window.dispatchEvent(new CustomEvent(DATA_SYNCED_EVENT));

    return { synced: true, count: (data.emails || []).length };
  } catch (err) {
    console.error("[v0] Sync: runActivitySync failed:", err);
    return { synced: false, reason: "network_error" };
  } finally {
    // Always release the single-flight guard so the NEXT eligible sync (after
    // the cooldown) can run.
    _syncInFlight = false;
  }
}
