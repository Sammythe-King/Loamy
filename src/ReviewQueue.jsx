import React, { useState, useEffect, useCallback } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faBell, faTimes, faPaperPlane, faCheckCircle } from "@fortawesome/free-solid-svg-icons";
import {
  fetchReviewQueue,
  resolveReviewItem,
  dismissReviewItem,
  seedDemoReview,
} from "./services/reviewQueueService";
import "./ReviewQueue.css";

/*
 * ReviewQueue - Layer 2 "Chat Nudge" UI.
 * Single responsibility: display unrecognized bank transactions as casual chat
 * questions and let the user reply. All API logic lives in reviewQueueService.
 */
const ReviewQueue = ({ userId = "default", userName = "there" }) => {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [replies, setReplies] = useState({}); // reviewId -> draft text
  const [submittingId, setSubmittingId] = useState(null);
  const [resolvedNote, setResolvedNote] = useState(null);

  const loadQueue = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchReviewQueue(userId);
      if (res.status === "success") {
        setItems(res.data?.items || []);
      } else {
        setError(res.error || "Could not load the review queue.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  }, [userId]);

  // Initial load + lightweight polling so new nudges appear (notification-ready).
  useEffect(() => {
    loadQueue();
    const interval = setInterval(loadQueue, 30000);
    return () => clearInterval(interval);
  }, [loadQueue]);

  const handleReplyChange = (id, value) => {
    setReplies((prev) => ({ ...prev, [id]: value }));
  };

  const handleSubmit = async (id) => {
    const text = (replies[id] || "").trim();
    if (!text) return;
    setSubmittingId(id);
    setError(null);
    try {
      const res = await resolveReviewItem(id, text);
      if (res.status === "success") {
        setResolvedNote(res.data?.confirmation || "Logged.");
        setItems((prev) => prev.filter((it) => it.id !== id));
        setReplies((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        setTimeout(() => setResolvedNote(null), 4000);
      } else {
        setError(res.error || "Could not save your reply.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setSubmittingId(null);
    }
  };

  const handleDismiss = async (id) => {
    try {
      await dismissReviewItem(id);
      setItems((prev) => prev.filter((it) => it.id !== id));
    } catch {
      setError("Could not dismiss this item.");
    }
  };

  const handleSeedDemo = async () => {
    setLoading(true);
    await seedDemoReview(userId, userName);
    await loadQueue();
  };

  const count = items.length;

  return (
    <div className="review-queue">
      <button
        type="button"
        className={`review-bell ${count > 0 ? "has-items" : ""}`}
        onClick={() => setOpen((o) => !o)}
        aria-label={`Review Queue, ${count} item${count === 1 ? "" : "s"} needing review`}
        aria-expanded={open}
      >
        <FontAwesomeIcon icon={faBell} />
        {count > 0 && <span className="review-badge">{count}</span>}
      </button>

      {open && (
        <>
          <div className="review-overlay" onClick={() => setOpen(false)} />
          <div className="review-panel" role="dialog" aria-label="Review Queue">
            <div className="review-panel-header">
              <div>
                <h3>Review Queue</h3>
                <p>Transactions Loamy needs your help with</p>
              </div>
              <button
                type="button"
                className="review-close"
                onClick={() => setOpen(false)}
                aria-label="Close review queue"
              >
                <FontAwesomeIcon icon={faTimes} />
              </button>
            </div>

            <div className="review-panel-body">
              {resolvedNote && (
                <div className="review-resolved-note">
                  <FontAwesomeIcon icon={faCheckCircle} /> {resolvedNote}
                </div>
              )}

              {loading && <div className="review-state">Loading...</div>}

              {error && !loading && (
                <div className="review-state review-error">{error}</div>
              )}

              {!loading && !error && count === 0 && (
                <div className="review-empty">
                  <FontAwesomeIcon icon={faCheckCircle} className="review-empty-icon" />
                  <p>You&apos;re all caught up!</p>
                  <span>
                    When Loamy catches an unrecognized bank transaction, it&apos;ll
                    ask you about it here.
                  </span>
                  <button type="button" className="review-demo-btn" onClick={handleSeedDemo}>
                    Try a sample nudge
                  </button>
                </div>
              )}

              {!loading &&
                items.map((item) => (
                  <div key={item.id} className="review-item">
                    <div className="review-bubble">
                      <div className="review-avatar">L</div>
                      <p>{item.nudge}</p>
                    </div>
                    <div className="review-meta">
                      <span className="review-amount">
                        &#8358;{Number(item.amount).toLocaleString()}
                      </span>
                      {item.date && <span className="review-date">{item.date}</span>}
                    </div>
                    <div className="review-reply-row">
                      <input
                        type="text"
                        placeholder="e.g. Sugar for stock"
                        value={replies[item.id] || ""}
                        onChange={(e) => handleReplyChange(item.id, e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && handleSubmit(item.id)}
                        disabled={submittingId === item.id}
                      />
                      <button
                        type="button"
                        className="review-send"
                        onClick={() => handleSubmit(item.id)}
                        disabled={submittingId === item.id || !(replies[item.id] || "").trim()}
                        aria-label="Send reply"
                      >
                        {submittingId === item.id ? (
                          <span className="review-spinner" />
                        ) : (
                          <FontAwesomeIcon icon={faPaperPlane} />
                        )}
                      </button>
                    </div>
                    <button
                      type="button"
                      className="review-dismiss"
                      onClick={() => handleDismiss(item.id)}
                    >
                      Dismiss
                    </button>
                  </div>
                ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
};

export default ReviewQueue;
