import { useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faChartPie, faComment, faFileInvoiceDollar,
  faListCheck, faCheckCircle, faBuildingColumns
} from "@fortawesome/free-solid-svg-icons";
import {
  fetchReviewQueue,
  resolveReviewItem,
  dismissReviewItem,
  seedDemoReview,
  confirmTransferPair,
  setTransferDestination,
  rejectTransfer,
} from "./services/reviewQueueService";
import ReviewTabs from "./components/reviewQueue/ReviewTabs";
import ExpenseCard from "./components/reviewQueue/ExpenseCard";
import TransferCard from "./components/reviewQueue/TransferCard";
import ActivityList from "./components/reviewQueue/ActivityList";
import Pagination, { paginate } from "./components/reviewQueue/Pagination";
import "./InvoicesPage.css";
import "./ReviewQueuePage.css";
import "./ReviewQueueTabs.css";

/*
 * ReviewQueuePage - full-page Review Queue with two tabs:
 * Review Queue (expenses, credits and unconfirmed transfers that need the
 * user) and Internal Transfers (the log of transfers Loamy auto-linked or
 * the user confirmed).
 * All API logic lives in reviewQueueService.
 */
const RECENT_PAGE_SIZE = 8;
const ACTIVITY_PAGE_SIZE = 10;

const vendorInitial = (vendor) => {
  const match = (vendor || "").match(/[A-Za-z0-9]/);
  return match ? match[0].toUpperCase() : "#";
};

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

  const [activeTab, setActiveTab] = useState("review");
  const [items, setItems] = useState([]);
  const [transfers, setTransfers] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [activity, setActivity] = useState([]);
  const [destinations, setDestinations] = useState({});
  const [resolved, setResolved] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [submittingId, setSubmittingId] = useState(null);
  const [resolvedNote, setResolvedNote] = useState(null);
  const [recentPage, setRecentPage] = useState(1);
  const [activityPage, setActivityPage] = useState(1);

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
        setTransfers(res.data?.transfers || []);
        setAccounts(res.data?.accounts || []);
        setActivity(res.data?.activity || []);
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

  const showNote = (note) => {
    setResolvedNote(note);
    setTimeout(() => setResolvedNote(null), 4000);
  };

  // Returns Loamy's follow-up question when the note was too vague, else null.
  const submitResolution = async (id, reply, category, history) => {
    setSubmittingId(id);
    setError(null);
    try {
      const res = await resolveReviewItem(id, reply, category, history);
      if (res.status !== "success") {
        setError(res.error || "Could not save your choice.");
        return "Sorry, I couldn't save that. Please try again.";
      }
      if (res.data?.resolved === false) return res.data.reply;
      showNote(res.data?.confirmation || "Logged.");
      if (res.data?.moved_to === "transfers") {
        setItems((prev) => prev.filter((it) => it.id !== id));
        await loadQueue(false);
        return null;
      }
      const logged = items.find((it) => it.id === id);
      if (logged) {
        setResolved((prev) => [
          { ...logged, category: res.data?.category || category, user_reply: reply, resolved_at: new Date().toISOString() },
          ...prev,
        ]);
      }
      setItems((prev) => prev.filter((it) => it.id !== id));
      return null;
    } catch {
      setError("Network error. Please try again.");
      return "I couldn't reach the server. Please try again.";
    } finally {
      setSubmittingId(null);
    }
  };

  const handleResolve = (id, category) => (category ? submitResolution(id, "", category) : null);
  const handleReply = (id, text, history) => submitResolution(id, text, "", history);

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

  const runTransferAction = async (id, action, successNote) => {
    setSubmittingId(id);
    setError(null);
    try {
      const res = await action();
      if (res.status === "success") {
        showNote(successNote);
        await loadQueue(false);
      } else {
        setError(res.error || "Could not update this transfer.");
      }
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setSubmittingId(null);
    }
  };

  const handleConfirmTransfer = (id) =>
    runTransferAction(id, () => confirmTransferPair(id), "Marked as a transfer between your own accounts.");

  const handleRejectTransfer = (id) =>
    runTransferAction(id, () => rejectTransfer(id), "Kept in your Review Queue as a normal transaction.");

  const handleUndo = (id) =>
    runTransferAction(id, () => rejectTransfer(id), "Undone. It's back in your Review Queue.");

  const handleDestination = (id) => {
    const destination = destinations[id];
    if (!destination) return;
    runTransferAction(id, () => setTransferDestination(id, destination), "Transfer linked to your account.");
  };

  // A candidate pair has two legs in the queue: show it once.
  const transferCards = (() => {
    const seen = new Set();
    return transfers.filter((t) => {
      if (t.transfer_stage !== "candidate" || !t.pair_id) return true;
      if (seen.has(t.pair_id)) return false;
      seen.add(t.pair_id);
      return true;
    });
  })();

  // Confirmed transfers live in the Internal Transfers tab, not in "Recently Logged".
  const recentExpenses = resolved.filter((it) => !/internal transfer/i.test(it.category || ""));

  const recentPaged = paginate(recentExpenses, recentPage, RECENT_PAGE_SIZE);
  const activityPaged = paginate(activity, activityPage, ACTIVITY_PAGE_SIZE);

  const expenseCount = items.length;
  const transferCount = transferCards.length;
  const count = expenseCount + transferCount;

  const renderReview = () => (
    <>
      <section aria-labelledby="rqt-needs-review">
        <div className="rqt-section-head">
          <h2 id="rqt-needs-review">Needs Review</h2>
          {count > 0 && <span className="rqt-count">{count}</span>}
        </div>
        {count > 0 ? (
          <div className="rqt-grid">
            {transferCards.map((t) => (
              <TransferCard
                key={t.id}
                transfer={t}
                accounts={accounts}
                destination={destinations[t.id]}
                busy={submittingId === t.id}
                onDestinationChange={(id, value) => setDestinations((prev) => ({ ...prev, [id]: value }))}
                onConfirm={handleConfirmTransfer}
                onLink={handleDestination}
                onReject={handleRejectTransfer}
              />
            ))}
            {items.map((item) => (
              <ExpenseCard
                key={item.id}
                item={item}
                busy={submittingId === item.id}
                onResolve={handleResolve}
                onReply={handleReply}
                onDismiss={handleDismiss}
              />
            ))}
          </div>
        ) : (
          <div className="rqt-empty">
            <FontAwesomeIcon icon={faCheckCircle} className="rqt-empty-icon" />
            <div className="rqt-empty-text">
              <h3>You&apos;re all caught up!</h3>
              <p>When Loamy can&apos;t categorize a bank transaction on its own, it will show up here.</p>
            </div>
            {recentExpenses.length === 0 && (
              <button type="button" className="rq-demo-btn" onClick={handleSeedDemo}>
                Try a sample nudge
              </button>
            )}
          </div>
        )}
      </section>

      {recentExpenses.length > 0 && (
        <section className="rqt-recent" aria-labelledby="rqt-recent-title">
          <div className="rqt-section-head">
            <h2 id="rqt-recent-title">Recently Logged</h2>
            <span className="rqt-count is-muted">{recentExpenses.length}</span>
          </div>
          <ul className="rqt-recent-list">
            {recentPaged.pageItems.map((item) => (
              <li key={item.id} className="rqt-recent-row">
                <span className="rqt-recent-avatar" aria-hidden="true">
                  {vendorInitial(item.vendor)}
                </span>
                <div className="rqt-recent-main">
                  <span className="rqt-recent-vendor" title={item.vendor}>{item.vendor || "Transaction"}</span>
                  <span className="rqt-recent-date">{item.date}</span>
                </div>
                {item.category && <span className="rqt-recent-tag">{item.category}</span>}
                <span
                  className={`rqt-recent-amount ${item.transaction_type === "credit" ? "is-credit" : "is-debit"}`}
                >
                  {item.transaction_type === "credit" ? "+" : "\u2212"}&#8358;
                  {Number(item.amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </span>
              </li>
            ))}
          </ul>
          <Pagination {...recentPaged} onChange={setRecentPage} label="Recently logged" />
        </section>
      )}
    </>
  );

  const renderTransfers = () => (
    <section aria-labelledby="rqt-activity-title">
      <div className="rqt-section-head">
        <h2 id="rqt-activity-title">Internal Transfers</h2>
        {activity.length > 0 && <span className="rqt-count is-muted">{activity.length}</span>}
      </div>
      <p className="rqt-section-sub">
        Money moved between your own accounts. Loamy logs obvious ones automatically and keeps them out of your spending and income. Undo sends one back to your Review Queue.
      </p>
      {activity.length > 0 ? (
        <>
          <ActivityList entries={activityPaged.pageItems} busyId={submittingId} onUndo={handleUndo} />
          <Pagination {...activityPaged} onChange={setActivityPage} label="System activity" />
        </>
      ) : (
        <div className="rqt-empty">
          <div className="rqt-empty-text">
            <h3>Nothing automated yet</h3>
            <p>When Loamy links a transfer between your accounts, you&apos;ll see it here.</p>
          </div>
        </div>
      )}
    </section>
  );

  const panels = { review: renderReview, transfers: renderTransfers };

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
            <Link to="/accounts" className="menu-item">
              <FontAwesomeIcon icon={faBuildingColumns} />
              <span>Bank Accounts</span>
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
            <p>Transactions and transfers Loamy needs your help with</p>
          </div>
        </header>

        {resolvedNote && (
          <div className="rq-resolved-note" role="status">
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

        {!loading && !error && (
          <div className="rqt-shell">
            <ReviewTabs
              active={activeTab}
              onChange={setActiveTab}
              pending={{ review: count }}
            />
            <div
              className="rqt-panel"
              role="tabpanel"
              id={`rqt-panel-${activeTab}`}
              aria-labelledby={`rqt-tab-${activeTab}`}
            >
              {panels[activeTab]()}
            </div>
          </div>
        )}
      </main>
    </div>
  );
};

export default ReviewQueuePage;
