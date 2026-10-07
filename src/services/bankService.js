// Bank management service layer: all /api/v1 bank + account calls live here
// so UI components stay presentational.

import { isRealUserId } from "./activitySyncService";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

async function request(path, options) {
  let res;
  try {
    res = await fetch(`${API_URL}${path}`, options);
  } catch {
    return { status: "error", data: null, error: "Network error. Please try again." };
  }
  let body;
  try {
    body = await res.json();
  } catch {
    return { status: "error", data: null, error: "Invalid server response" };
  }
  // FastAPI HTTPException responses come back as { detail: "..." }.
  if (!res.ok) {
    return { status: "error", data: null, error: body?.detail || body?.error || `Request failed (${res.status})` };
  }
  return body;
}

export function getSessionUserId() {
  try {
    const session = JSON.parse(localStorage.getItem("loamy_session") || "{}");
    return session.user_id || "default";
  } catch {
    return "default";
  }
}

export function fetchAvailableBanks() {
  return request("/api/v1/banks/available");
}

export function fetchConnectedAccounts(userId) {
  if (!isRealUserId(userId)) {
    return Promise.resolve({ status: "error", data: null, error: "no_session" });
  }
  return request(`/api/v1/accounts?user_id=${encodeURIComponent(userId)}`);
}

export function connectBank(userId, bankSlug, { accountTail = "", nickname = "" } = {}) {
  return request("/api/v1/accounts/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      bank_slug: bankSlug,
      account_tail: accountTail,
      nickname,
    }),
  });
}

export function fetchAccountTransactions(userId, accountId, filters = {}) {
  if (!isRealUserId(userId)) {
    return Promise.resolve({ status: "error", data: null, error: "no_session" });
  }
  const params = new URLSearchParams({ user_id: userId });
  if (filters.type) params.set("type", filters.type);
  if (filters.startDate) params.set("start_date", filters.startDate);
  if (filters.endDate) params.set("end_date", filters.endDate);
  if (filters.limit) params.set("limit", String(filters.limit));
  return request(`/api/v1/accounts/${encodeURIComponent(accountId)}/transactions?${params}`);
}

export function formatNaira(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("en-NG", {
    style: "currency",
    currency: "NGN",
    maximumFractionDigits: 2,
  }).format(Number(value));
}
