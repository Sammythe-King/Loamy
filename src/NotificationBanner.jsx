import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { fetchNotifications, dismissNotification } from "./services/notificationsService";
import "./NotificationBanner.css";

/*
 * NotificationBanner
 * Single responsibility: surface active in-app notifications (e.g. the daily
 * 5 PM invoice reminder) as a dismissible banner. All API logic lives in
 * notificationsService - this component only decides how it looks.
 */
const NotificationBanner = ({ userId = "default" }) => {
  const navigate = useNavigate();
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await fetchNotifications(userId);
      if (res.status === "success") {
        setItems(res.data?.items || []);
        setError(null);
      } else {
        setError(res.error || "Could not load notifications.");
      }
    } catch {
      setError("Network error while loading notifications.");
    }
  }, [userId]);

  useEffect(() => {
    load();
    // Poll periodically so the 5 PM reminder appears without a manual refresh.
    const interval = setInterval(load, 60000);
    return () => clearInterval(interval);
  }, [load]);

  const handleDismiss = async (id) => {
    // Optimistically remove, then persist.
    setItems((prev) => prev.filter((it) => it.id !== id));
    try {
      await dismissNotification(id);
    } catch {
      // If it fails, a future poll will simply re-show it.
    }
  };

  const handleAction = async (item) => {
    await handleDismiss(item.id);
    if (item.action === "create_invoice") {
      navigate("/invoices");
    }
  };

  if (error) return null; // Stay silent on errors - notifications are non-critical.
  if (items.length === 0) return null;

  return (
    <div className="notification-stack">
      {items.map((item) => (
        <div key={item.id} className={`notification-banner ${item.type}`} role="status">
          <div className="notification-icon">
            <i className="fa-solid fa-bell"></i>
          </div>
          <div className="notification-body">
            <span className="notification-title">{item.title}</span>
            <span className="notification-message">{item.message}</span>
          </div>
          <div className="notification-actions">
            {item.action === "create_invoice" && (
              <button className="notification-btn primary" onClick={() => handleAction(item)}>
                Log an invoice
              </button>
            )}
            <button className="notification-btn ghost" onClick={() => handleDismiss(item.id)}>
              Not now
            </button>
          </div>
        </div>
      ))}
    </div>
  );
};

export default NotificationBanner;
