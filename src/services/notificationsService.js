// Notifications service layer.
// All network/API logic for in-app notifications lives here so UI components
// only care about "how it looks", not "where the data comes from".

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return { status: "error", data: null, error: "Invalid server response" };
  }
}

// Fetch all active (non-dismissed) notifications for a user.
export async function fetchNotifications(userId = "default") {
  const res = await fetch(`${API_URL}/notifications?user_id=${encodeURIComponent(userId)}`);
  return safeJson(res);
}

// Dismiss a notification so it stops showing.
export async function dismissNotification(notificationId) {
  const res = await fetch(`${API_URL}/notifications/dismiss`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notification_id: notificationId }),
  });
  return safeJson(res);
}
