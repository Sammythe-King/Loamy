import { useEffect, useRef } from "react";
import { runActivitySync } from "../services/activitySyncService";

/**
 * useGlobalActivitySync
 *
 * Tracks user activity across the ENTIRE app (not a single page) by attaching
 * debounced listeners to the global `window` object. When activity is detected
 * it sends a lightweight ping to the backend, which owns the real cooldown /
 * throttle logic (a 5-minute shield) and decides whether to run a sync.
 *
 * Single responsibility: detect activity + debounce + fire a ping. It does NOT
 * know how the request is made (that's the service) or what the backend does
 * with it (that's FastAPI).
 *
 * @param {Object}  options
 * @param {boolean} options.enabled   Turn tracking on/off (e.g. only when logged in).
 * @param {number}  options.debounceMs Client-side debounce window in ms.
 */
export function useGlobalActivitySync({ enabled = true, debounceMs = 2000 } = {}) {
  // Holds the pending debounce timer between renders without causing re-renders.
  const timerRef = useRef(null);
  // Guards against overlapping in-flight pings.
  const sendingRef = useRef(false);

  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;

    // Run a sync. The service enforces the 5-minute cooldown internally, so
    // activity-driven calls are cheap no-ops most of the time. `force` bypasses
    // the cooldown for "the user just arrived" moments (mount / tab return).
    const fireSync = async (force = false) => {
      if (sendingRef.current) return;
      sendingRef.current = true;
      try {
        await runActivitySync({ force });
      } finally {
        sendingRef.current = false;
      }
    };

    // Debounce: collapse bursts of mousemove/keydown/click into a single check.
    const handleActivity = () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => fireSync(false), debounceMs);
    };

    // When the user switches back to this tab, run a cooldown-respecting sync.
    // We deliberately do NOT force here: forcing would bypass the 5-minute
    // shield and, if the dev server watches the DB files, every resulting write
    // could trigger a page reload -> remount -> sync -> reload loop.
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        if (timerRef.current) clearTimeout(timerRef.current);
        fireSync(false);
      }
    };

    const activityEvents = ["mousemove", "keydown", "click"];
    activityEvents.forEach((evt) =>
      window.addEventListener(evt, handleActivity, { passive: true })
    );
    document.addEventListener("visibilitychange", handleVisibility);

    // Run one cooldown-respecting sync on mount. If the app hasn't synced in the
    // last 5 minutes (e.g. "I haven't used it since yesterday"), this pulls
    // fresh data exactly once. Because it respects the cooldown, a remount/
    // reload cannot immediately re-trigger it, so there is no reload loop.
    fireSync(false);

    return () => {
      activityEvents.forEach((evt) =>
        window.removeEventListener(evt, handleActivity)
      );
      document.removeEventListener("visibilitychange", handleVisibility);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [enabled, debounceMs]);
}
