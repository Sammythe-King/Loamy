// Review Queue service layer.
// All network/API logic for the Review Queue lives here so UI components only
// care about "how it looks", not "where the data comes from".

import { isRealUserId } from "./activitySyncService";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return { status: "error", data: null, error: "Invalid server response" };
  }
}

// Fetch all pending review items for a user.
export async function fetchReviewQueue(userId) {
  if (!isRealUserId(userId)) {
    return { status: "error", data: null, error: "no_session" };
  }
  const res = await fetch(`${API_URL}/review-queue/${encodeURIComponent(userId)}`);
  return safeJson(res);
}

// Submit the user's free-text reply to a nudge. The backend extracts the
// category, tags the transaction, and saves a vendor memory rule.
// Pass `category` to apply a category picked from the dropdown directly.
// `history` carries the user's earlier messages so a multi-message explanation
// is read as one.
export async function resolveReviewItem(reviewId, reply, category, history) {
  const res = await fetch(`${API_URL}/review-queue/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      review_id: reviewId,
      reply: reply || "",
      category: category || "",
      history: history || [],
    }),
  });
  return safeJson(res);
}

async function postTransferAction(path, payload) {
  const res = await fetch(`${API_URL}/review-queue/transfer/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return safeJson(res);
}

// Confirm a staged pair as one transfer between the user's own accounts.
export const confirmTransferPair = (reviewId) =>
  postTransferAction("confirm", { review_id: reviewId });

// One-sided debit/credit: say which of your own accounts it went to / came from.
export const setTransferDestination = (reviewId, destination) =>
  postTransferAction("destination", { review_id: reviewId, destination });

// "Not a transfer": send it back to the Expense Queue as a real expense/income.
export const rejectTransfer = (reviewId) =>
  postTransferAction("reject", { review_id: reviewId });

// Dismiss/ignore a review item without categorizing it.
export async function dismissReviewItem(reviewId) {
  const res = await fetch(`${API_URL}/review-queue/dismiss`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ review_id: reviewId }),
  });
  return safeJson(res);
}

// Seed a demo nudge so the user can try the flow before real bank emails arrive.
export async function seedDemoReview(userId, userName) {
  const res = await fetch(`${API_URL}/review-queue/seed-demo`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, user_name: userName }),
  });
  return safeJson(res);
}
