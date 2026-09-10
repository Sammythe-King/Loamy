import { useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faChartPie, faComment, faFileInvoiceDollar,
  faListCheck, faPaperPlane, faCheckCircle
} from "@fortawesome/free-solid-svg-icons";
import {
  fetchReviewQueue,
  resolveReviewItem,
  dismissReviewItem,
  seedDemoReview,
} from "./services/reviewQueueService";
import "./InvoicesPage.css";
import "./ReviewQueuePage.css";

/*
 * ReviewQueuePage - full-page version of the Review Queue.
 * Single responsibility: render the sidebar + the queue of unrecognized
 * transactions as chat nudges. All API logic lives in reviewQueueService.
 */
const ReviewQueuePage = () => {
  // Use the REAL logged-in user, not a hardcoded "default". The ChatPage widget
  // and dashboard key everything to the session user_id (e.g. user_377bad...),
  // so hardcoding "default" here put this page on an orphaned partition that
  // never reflected the rest of the app. Read it once from the session.
  const [{ userId, userName }] = useState(() => {
    try {
      const session = JSON.parse(localStorage.getItem("loamy_session") || "{}");
      return {
        userId: session.user_id || "default",
        userName: session.name || "there",
      };
    } catch {
      return { userId: "default", userName: "there" };
    }
  });

  const [items, setItems] = useState([]);
  const [resolved, setResolved] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [replies, setReplies] = useState({});
  const [submittingId, setSubmittingId] = useState(null);
  const [resolvedNote, setResolvedNote] = useState(null);

  // isInitial=true shows the full-page "Loading..." state (first mount only).
  // The 30s poll below reuses this same function to pick up new nudges, but
  // must NOT re-arm that full-page loading state - doing so wiped the whole
  // list back to a blank "Loading..." screen every 30 seconds, which is why
  // the page looked stuck.
  const loadQueue = useCallback(async (isInitial = false) => {
    if (isInitial) setLoading(true);
    setError(null);
    try {
      const res = await fetchReviewQueue(userId);
      if (res.status === "success") {
        setItems(res.data?.items || []);
        setResolved(res.data?.resolved || []);
      } else {
        setError(res.error || "Could not load the review queue.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      if (isInitial) setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    loadQueue(true);
    const interval = setInterval(() => loadQueue(false), 30000);
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
        const logged = items.find((it) => it.id === id);
        if (logged) {
          // Instantly show it in the "Recently logged" history for visual impact.
          setResolved((prev) => [
            {
              ...logged,
              category: res.data?.category || "Other",
              user_reply: text,
              resolved_at: new Date().toISOString(),
            },
            ...prev,
          ]);
        }
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
    await seedDemoReview(userId, userName);
    await loadQueue(true);
  };

  const count = items.length;

  return (
    <div className="invoices-page">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <h2>Loamy</h2>
        </div>

        <div className="sidebar-menu">
          <div className="menu-section">
            <p className="section-label">Tools</p>
            <Link to="/spending" className="menu-item">
              <FontAwesomeIcon icon={faChartPie} />
              <span>Spending Analysis</span>
            </Link>
            <Link to="/chat" className="menu-item">
              <FontAwesomeIcon icon={faComment} />
              <span>Chat</span>
            </Link>
            <Link to="/invoices" className="menu-item">
              <FontAwesomeIcon icon={faFileInvoiceDollar} />
              <span>Invoices</span>
            </Link>
            <Link to="/review-queue" className="menu-item active">
              <FontAwesomeIcon icon={faListCheck} />
              <span>Review Queue</span>
              {count > 0 && <span className="menu-badge">{count}</span>}
            </Link>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="invoices-main">
        <header className="invoices-header">
          <div>
            <h1>Review Queue</h1>
            <p>Transactions Loamy needs your help categorizing</p>
          </div>
        </header>

        {resolvedNote && (
          <div className="rq-resolved-note">
            <FontAwesomeIcon icon={faCheckCircle} /> {resolvedNote}
          </div>
        )}

        {loading && (
          <div className="rq-state">
            <div className="loamy-loader">
              <div className="loamy-loader-bar" role="progressbar" aria-label="Loading review queue">
                <span />
              </div>
              <p className="loamy-loader-text">Loading your financial review...</p>
            </div>
          </div>
        )}

        {error && !loading && <div className="rq-state rq-error">{error}</div>}

        {!loading && !error && count === 0 && resolved.length === 0 && (
          <div className="rq-empty">
            <FontAwesomeIcon icon={faCheckCircle} className="rq-empty-icon" />
            <h3>You&apos;re all caught up!</h3>
            <p>
              When Loamy catches an unrecognized bank transaction, it&apos;ll ask
              you about it here so you can tell it what the payment was for.
            </p>
            <button type="button" className="rq-demo-btn" onClick={handleSeedDemo}>
              Try a sample nudge
            </button>
          </div>
        )}

        {!loading && !error && count === 0 && resolved.length > 0 && (
          <div className="rq-caughtup-banner">
            <FontAwesomeIcon icon={faCheckCircle} />
            <span>You&apos;re all caught up! Nothing left to review right now.</span>
          </div>
        )}

        {!loading && !error && (count > 0 || resolved.length > 0) && (
        <div className={`rq-layout ${resolved.length > 0 ? "has-aside" : ""}`}>
        <div className="rq-primary">
        {count > 0 && (
          <div className="rq-list">
            {items.map((item) => (
              <div key={item.id} className="rq-card">
                <div className="rq-bubble">
                  <div className="rq-avatar">L</div>
                  <p>{item.nudge}</p>
                </div>
                <div className="rq-meta">
                  <span className="rq-amount">
                    &#8358;{Number(item.amount).toLocaleString()}
                  </span>
                  {item.date && <span className="rq-date">{item.date}</span>}
                </div>
                <div className="rq-reply-row">
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
                    className="rq-send"
                    onClick={() => handleSubmit(item.id)}
                    disabled={submittingId === item.id || !(replies[item.id] || "").trim()}
                    aria-label="Send reply"
                  >
                    {submittingId === item.id ? (
                      <span className="rq-spinner" />
                    ) : (
                      <FontAwesomeIcon icon={faPaperPlane} />
                    )}
                  </button>
                </div>
                <button
                  type="button"
                  className="rq-dismiss"
                  onClick={() => handleDismiss(item.id)}
                >
                  Dismiss
                </button>
              </div>
            ))}
          </div>
        )}
        </div>

        {resolved.length > 0 && (
          <aside className="rq-aside">
          <section className="rq-history">
            <div className="rq-history-head">
              <h2>Recently logged</h2>
              <span className="rq-history-count">{resolved.length}</span>
            </div>
            <p className="rq-history-sub">
              Expenses you&apos;ve categorized from your review queue.
            </p>
            <ul className="rq-history-list">
              {resolved.map((item) => (
                <li key={item.id} className="rq-history-item">
                  <div className="rq-history-check">
                    <FontAwesomeIcon icon={faCheckCircle} />
                  </div>
                  <div className="rq-history-body">
                    <div className="rq-history-row">
                      <span className="rq-history-vendor">
                        {item.vendor || "Transaction"}
                      </span>
                      <span className="rq-history-amount">
                        &#8358;{Number(item.amount).toLocaleString()}
                      </span>
                    </div>
                    <div className="rq-history-meta">
                      {item.category && (
                        <span className="rq-history-tag">{item.category}</span>
                      )}
                      {item.user_reply && (
                        <span className="rq-history-note">
                          &ldquo;{item.user_reply}&rdquo;
                        </span>
                      )}
                      {item.date && (
                        <span className="rq-history-date">{item.date}</span>
                      )}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </section>
          </aside>
        )}
        </div>
        )}
      </main>
    </div>
  );
};

export default ReviewQueuePage;
