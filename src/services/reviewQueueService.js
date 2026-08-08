// Review Queue service layer.
// All network/API logic for the Review Queue lives here so UI components only
// care about "how it looks", not "where the data comes from".

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
  const res = await fetch(`${API_URL}/review-queue/${encodeURIComponent(userId)}`);
  return safeJson(res);
}

// Submit the user's free-text reply to a nudge. The backend extracts the
// category, tags the transaction, and saves a vendor memory rule.
export async function resolveReviewItem(reviewId, reply) {
  const res = await fetch(`${API_URL}/review-queue/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ review_id: reviewId, reply }),
  });
  return safeJson(res);
}

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
