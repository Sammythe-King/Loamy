/*
 * Per-user persistence for the dashboard product tour.
 *
 * Status lives in localStorage under a user-specific key so one account's
 * tour state never affects another's on a shared browser:
 *
 *   loamy_tour_status_<userId> = "completed" | "skipped"
 *
 * The backend user model (ChromaDB users_collection) has no tour field and
 * the schema must not change, so localStorage is the store. Keys survive
 * refreshes and browser restarts, and Dashboard's logout handler explicitly
 * preserves them across its localStorage.clear() call.
 */

export const TOUR_STORAGE_PREFIX = "loamy_tour_status_";

export const TOUR_STATUS = {
  COMPLETED: "completed",
  SKIPPED: "skipped",
};

const storageKey = (userId) => TOUR_STORAGE_PREFIX + (userId || "default");

/** @returns {"completed" | "skipped" | null} */
export const getTourStatus = (userId) => {
  try {
    return localStorage.getItem(storageKey(userId));
  } catch {
    // Private mode / storage disabled → treat as "never seen".
    return null;
  }
};

export const setTourStatus = (userId, status) => {
  try {
    localStorage.setItem(storageKey(userId), status);
  } catch {
    // Storage unavailable — the tour will simply be offered again next visit.
  }
};