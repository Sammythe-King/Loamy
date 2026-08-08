import React, { useState, useEffect } from "react";
import "./Dashboard.css";
import { useNavigate } from "react-router-dom";
import { PieChart, Pie, Cell, ResponsiveContainer } from "recharts";
import NotificationBanner from "./NotificationBanner";
import { runActivitySync } from "./services/activitySyncService";

const API_URL = "http://127.0.0.1:8000";

// Single source of truth for "who is logged in". Every dashboard data request
// must be scoped to this id, otherwise the backend falls back to a shared
// "default" bucket and a new user would see the previous user's data.
const getSessionUserId = () => {
  try {
    const sess = JSON.parse(localStorage.getItem("loamy_session") || "{}");
    return sess.user_id || "default";
  } catch {
    return "default";
  }
};

// Progress Bar Component
const ProgressBar = ({ value }) => {
  return (
    <div style={{
      width: "100%",
      height: "10px",
      background: "#e5e7eb",
      borderRadius: "20px",
      margin: "10px 0",
      overflow: "hidden"
    }}>
      <div style={{
        width: `${Math.min(value, 100)}%`,
        height: "100%",
        background: "#f59e0b",
        transition: "width 0.3s linear",
      }} />
    </div>
  );
};

// Green palette for the expense "color wheel" — shades of green from dark to light
// so each slice is a distinct green tone instead of the mixed default colors.
// Distinct green shades for the expense wheel. Ordered with large
// light/dark jumps between neighbours so adjacent slices never blur together,
// while still reading as a single green family.
const EXPENSE_GREENS = [
  "#0B3D0B", // deepest forest
  "#66BB6A", // medium-light
  "#1B5E20", // dark
  "#A5D6A7", // mint
  "#2E7D32", // primary green
  "#C8E6C9", // palest
  "#388E3C", // mid
  "#81C784", // light
];

// Expense Chart Component
const ExpenseChart = ({ data }) => {
  const defaultData = [
    { name: "No Data", value: 100, color: "#e5e7eb" }
  ];

  // Re-color slices with distinct green shades by index, keeping a neutral grey
  // for the "No Data" placeholder.
  const chartData = (data && data.length > 0 ? data : defaultData).map((item, index) => ({
    ...item,
    color: item.name === "No Data" ? "#e5e7eb" : EXPENSE_GREENS[index % EXPENSE_GREENS.length],
  }));
  
  return (
    <div className="expense-chart-card">
      <div className="expense-chart-content">
        <div style={{ width: 140, height: 140 }}>
          <ResponsiveContainer>
            <PieChart>
              <Pie
                data={chartData}
                innerRadius={30}
                outerRadius={60}
                paddingAngle={2}
                dataKey="value"
                stroke="#ffffff"
                strokeWidth={2}
              >
                {chartData.map((item, index) => (
                  <Cell key={index} fill={item.color} />
                ))}
              </Pie>
            </PieChart>
          </ResponsiveContainer>
        </div>

        <div className="expense-legend">
          {chartData.map((item, index) => (
            <div key={index} className="expense-row">
              <div className="expense-left">
                <span
                  className="expense-dot"
                  style={{ backgroundColor: item.color }}
                />
                <span>{item.name}</span>
              </div>
              <span>{item.value}%</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

// Format currency with proper symbol
const formatCurrency = (amount, currency = "NGN") => {
  if (!amount && amount !== 0) return "₦0.00";
  const num = parseFloat(amount);
  const symbols = { NGN: "₦", USD: "$", EUR: "€", GBP: "£", MUR: "₨", INR: "₹" };
  const symbol = symbols[currency] || "₦";
  return `${symbol}${num.toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

// Format transaction amount with original currency if different
const formatTransactionAmount = (tx) => {
  const amount = parseFloat(tx.amount || 0);
  const originalAmount = parseFloat(tx.original_amount || amount);
  const originalCurrency = tx.original_currency || "NGN";
  
  if (originalCurrency !== "NGN" && originalAmount !== amount) {
    return `${formatCurrency(originalAmount, originalCurrency)} (₦${amount.toLocaleString("en-NG", { minimumFractionDigits: 2 })})`;
  }
  return formatCurrency(amount, "NGN");
};

// Format date
const formatDate = (dateStr) => {
  if (!dateStr) return "";
  try {
    return new Date(dateStr).toLocaleDateString("en-NG", {
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
};

// Get greeting based on time of day
const getGreeting = () => {
  const hour = new Date().getHours();
  if (hour < 12) return "Good Morning";
  if (hour < 17) return "Good Afternoon";
  return "Good Evening";
};

const Dashboard = () => {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [dashboardData, setDashboardData] = useState({
    accounts: [],
    cash_flow: { cash_in: 0, cash_out: 0, current_balance: 0 },
    expense_breakdown: [],
    recent_transactions: [],
    bank_transactions: [],
    needs_review: [],
    goals: [],
    summary: {}
  });
  const [categorizingId, setCategorizingId] = useState(null);
  // Remembers which chip a user just clicked so we can flash a "logged" state.
  const [loggedTx, setLoggedTx] = useState({}); // { [txId]: category }
  const [recentInvoices, setRecentInvoices] = useState([]);
  const [invoiceSummary, setInvoiceSummary] = useState({ total_unpaid: 0, total_paid: 0, unpaid_count: 0, paid_count: 0 });
  // True while a manual "Refresh Data" sync is pulling fresh Gmail data.
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    fetchDashboardData();
    fetchInvoices();

    // When the background sync pulls fresh bank emails, re-fetch the dashboard
    // so balances/transactions update without a manual refresh or reconnect.
    const handleDataSynced = () => {
      fetchDashboardData();
      fetchInvoices();
    };
    window.addEventListener("loamy:data-synced", handleDataSynced);
    return () => window.removeEventListener("loamy:data-synced", handleDataSynced);
  }, []);

  const fetchInvoices = async () => {
    try {
      const response = await fetch(`${API_URL}/get-invoices?user_id=${encodeURIComponent(getSessionUserId())}`);
      const data = await response.json();
      if (data.status === "success") {
        // Show the 5 most recent invoices (newest created first)
        const sorted = [...(data.invoices || [])].sort((a, b) =>
          (b.created_date || "").localeCompare(a.created_date || "")
        );
        setRecentInvoices(sorted.slice(0, 5));
        setInvoiceSummary(data.summary || {});
      }
    } catch (err) {
      console.error("Invoices fetch error:", err);
    }
  };

  // Manual "Refresh Data": prefer the backend server-side sync, which mints a
  // fresh access token from the stored refresh token. This works even when the
  // browser token has expired, so the user never has to reconnect Gmail just to
  // refresh. Falls back to the client-side sync only if the backend has no
  // stored credentials yet.
  const handleRefreshData = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const sess = JSON.parse(localStorage.getItem("loamy_session") || "{}");
      const userId = sess.user_id || "default";
      const res = await fetch(`${API_URL}/gmail/server-sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      });
      const json = await res.json();
      if (json?.data?.reason === "no_credentials") {
        await runActivitySync({ force: true, fullResync: true });
      }
    } catch (err) {
      console.error("[v0] Manual refresh sync failed:", err);
      try {
        await runActivitySync({ force: true, fullResync: true });
      } catch (_) {}
    } finally {
      await Promise.all([fetchDashboardData(), fetchInvoices()]);
      setRefreshing(false);
    }
  };

  const fetchDashboardData = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_URL}/get-dashboard-data?user_id=${encodeURIComponent(getSessionUserId())}`);
      const data = await response.json();
      
      if (data.status === "success") {
        setDashboardData(data);
        setError(null);
      } else {
        setError(data.error || "Failed to load dashboard data");
      }
    } catch (err) {
      console.error("Dashboard fetch error:", err);
      setError("Could not connect to server");
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = () => {
    localStorage.clear();
    navigate("/");
  };

  const handleNavigation = (path) => {
    navigate(path);
  };

  const { cash_flow, expense_breakdown, recent_transactions, bank_transactions = [], needs_review = [], goals, summary } = dashboardData;
  
  // Categorize a transaction that needs review
  const categorizeTransaction = async (transactionId, category) => {
    try {
      setCategorizingId(transactionId);
      const response = await fetch(`${API_URL}/categorize-transaction`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transaction_id: transactionId, category })
      });
      const data = await response.json();
      if (data.status === "success") {
        // Flash a "logged" confirmation on the card, then refresh after a beat
        // so the user clearly sees their click was recorded.
        setLoggedTx((prev) => ({ ...prev, [transactionId]: category }));
        setTimeout(() => {
          fetchDashboardData();
          setLoggedTx((prev) => {
            const next = { ...prev };
            delete next[transactionId];
            return next;
          });
        }, 1100);
      }
    } catch (err) {
      console.error("Error categorizing:", err);
    } finally {
      setCategorizingId(null);
    }
  };

  if (loading) {
    return (
      <div className="dashboard-loading">
        <div className="loading-spinner"></div>
        <p>Loading your financial overview...</p>
      </div>
    );
  }

  return (
    <div className="dashboard-container">
      {/* Navigation */}
      <nav className="dashboard-nav">
        <div className="nav-brand">
          <h3>Loamy</h3>
        </div>
        <div className="nav-actions">
          <button onClick={() => handleNavigation("/gmail-connect")} className="nav-btn">
            <i className="fa-regular fa-envelope"></i>
            <span>Gmail</span>
          </button>
          <button onClick={() => handleNavigation("/goals")} className="nav-btn">
            <i className="fa-solid fa-bullseye"></i>
            <span>Goals</span>
          </button>
          <button onClick={() => handleNavigation("/chat")} className="nav-btn">
            <i className="fa-solid fa-comments"></i>
            <span>Chat</span>
          </button>
          <button onClick={handleLogout} className="nav-btn logout-btn">
            <i className="fa-solid fa-right-from-bracket"></i>
          </button>
        </div>
      </nav>

      {/* Greeting */}
      <div className="greeting-section">
        <h1>{getGreeting()} <span role="img" aria-label="wave">👋</span></h1>
        <p>Here&apos;s your financial overview for today</p>
        {error && <p className="error-message">{error}</p>}
      </div>

      {/* In-app notifications (e.g. the daily 5 PM invoice reminder) */}
      <NotificationBanner userId={getSessionUserId()} />

      {/* Main Content Grid */}
      <div className="dashboard-grid">
        {/* Cash Flow Overview */}
        <div className="card cashflow-card">
          <div className="card-header">
            <i className="fa-solid fa-wallet" style={{ color: "green" }}></i>
            <h3>Cash Flow Overview</h3>
          </div>
          <div className="cashflow-grid">
            <div className="cashflow-item cash-in">
              <div className="cashflow-label">
                <i className="fa-solid fa-arrow-down" style={{ color: "green" }}></i>
                <span>Cash In</span>
              </div>
              <span className="cashflow-amount green">{formatCurrency(cash_flow.cash_in)}</span>
            </div>
            <div className="cashflow-item cash-out">
              <div className="cashflow-label">
                <i className="fa-solid fa-arrow-up" style={{ color: "red" }}></i>
                <span>Cash Out</span>
              </div>
              <span className="cashflow-amount red">{formatCurrency(cash_flow.cash_out)}</span>
            </div>
            <div className="cashflow-item current-cash">
              <div className="cashflow-label">
                <i className="fa-solid fa-wallet" style={{ color: "green" }}></i>
                <span>Current Balance</span>
              </div>
              <span className="cashflow-amount green">{formatCurrency(cash_flow.current_balance)}</span>
            </div>
          </div>
        </div>

        {/* Business Runway */}
        <div className="card runway-card">
          <div className="card-header">
            <i className="fa-solid fa-clock" style={{ color: "#f59e0b" }}></i>
            <h3>Business Runway</h3>
          </div>
          <span className="runway-days">{summary.runway_days || 0} Days</span>
          <p className="runway-text">
            At your current spending rate, your cash runway is approximately this many days.
          </p>
          <ProgressBar value={Math.min((summary.runway_days || 0) / 3.65, 100)} />
        </div>

        {/* Expense Breakdown */}
        <div className="card expense-card">
          <div className="card-header">
            <i className="fa-solid fa-chart-pie" style={{ color: "#2E7D32" }}></i>
            <h3>Expense Breakdown</h3>
          </div>
          <ExpenseChart data={expense_breakdown} />
          {expense_breakdown.length > 0 && (
            <div className="expense-insight">
              Top expense: {expense_breakdown[0]?.name} ({expense_breakdown[0]?.value}%)
            </div>
          )}
        </div>

        {/* Recent Invoices */}
        <div className="card accounts-card">
          <div className="card-header">
            <i className="fa-solid fa-file-invoice-dollar" style={{ color: "#225a24" }}></i>
            <h3>Recent Invoices</h3>
          </div>

          {recentInvoices.length > 0 ? (
            <>
              <div className="invoice-summary-row">
                <div className="invoice-summary-pill unpaid">
                  <span className="invoice-summary-label">Unpaid</span>
                  <span className="invoice-summary-value">{formatCurrency(invoiceSummary.total_unpaid || 0)}</span>
                </div>
                <div className="invoice-summary-pill paid">
                  <span className="invoice-summary-label">Paid</span>
                  <span className="invoice-summary-value">{formatCurrency(invoiceSummary.total_paid || 0)}</span>
                </div>
              </div>

              <div className="transactions-list">
                {recentInvoices.map((inv, index) => (
                  <div key={inv.id || index} className="transaction-item">
                    <div className="transaction-info">
                      <span className="transaction-desc">{inv.client_name || "Client"}</span>
                      <span className="transaction-date">
                        {inv.description ? `${inv.description.substring(0, 22)} - ` : ""}Due {formatDate(inv.due_date)}
                      </span>
                    </div>
                    <div className="invoice-item-right">
                      <span className="transaction-amount">{formatCurrency(inv.amount)}</span>
                      <span className={`invoice-status-badge ${inv.status}`}>
                        {inv.status === "paid" ? "Paid" : "Unpaid"}
                      </span>
                    </div>
                  </div>
                ))}
              </div>

              <button onClick={() => handleNavigation("/invoices")} className="view-all-btn">
                View all invoices
              </button>
            </>
          ) : (
            <div className="empty-state">
              <p>No invoices yet.</p>
              <button onClick={() => handleNavigation("/invoices")} className="action-btn">
                Create an invoice
              </button>
            </div>
          )}
        </div>

        {/* Recent Transactions */}
        <div className="card transactions-card">
          <div className="card-header">
            <i className="fa-solid fa-receipt" style={{ color: "#9C27B0" }}></i>
            <h3>Recent Transactions</h3>
          </div>
          {recent_transactions.length > 0 ? (
            <div className="transactions-list">
              {recent_transactions.slice(0, 5).map((tx, index) => (
                <div key={index} className="transaction-item">
                  <div className="transaction-info">
                    <span className="transaction-desc">{tx.description?.substring(0, 25) || "Transaction"}</span>
                    <span className="transaction-date">{formatDate(tx.date)} - {tx.category || tx.bank}</span>
                  </div>
                  <span className={`transaction-amount ${tx.type === "credit" ? "green" : "red"}`}>
                    {tx.type === "credit" ? "+" : "-"}{formatTransactionAmount(tx)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty-state">
              <p>No recent transactions found.</p>
            </div>
          )}
        </div>

      </div>

      {/* Bank Activity + Needs Review — shown side by side so users can see a
          transaction and immediately categorize it from the column beside it. */}
      <div className="activity-review-row">
        {/* Bank Activity */}
        <div className="card accounts-card activity-col">
          <div className="card-header">
            <i className="fa-solid fa-building-columns" style={{ color: "#1976D2" }}></i>
            <h3>Bank Activity</h3>
          </div>
          {bank_transactions.length > 0 ? (
            <>
              <p className="bank-activity-label">Your 10 most recent bank credits & debits</p>
              <div className="transactions-list">
                {bank_transactions.slice(0, 10).map((tx, index) => (
                  <div key={tx.id || index} className="transaction-item">
                    <div className="transaction-info">
                      <span className="transaction-desc">{(tx.description || "Transaction").substring(0, 30)}</span>
                      <span className="transaction-date">
                        {formatDate(tx.date)}{tx.bank ? ` - ${tx.bank}` : ""}
                      </span>
                    </div>
                    <span className={`transaction-amount ${tx.type === "credit" ? "green" : "red"}`}>
                      {tx.type === "credit" ? "+" : "-"}{formatTransactionAmount(tx)}
                    </span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className="empty-state">
              <p>No bank activity yet.</p>
              <button onClick={() => handleNavigation("/gmail-connect")} className="action-btn">
                Sync Gmail
              </button>
            </div>
          )}
        </div>

        {/* Needs Review */}
        {needs_review && needs_review.length > 0 ? (
          <div className="card needs-review-card review-col">
            <div className="card-header">
              <i className="fa-solid fa-circle-exclamation" style={{ color: "#F4A261" }}></i>
              <h3>Needs Review ({needs_review.length})</h3>
            </div>
            <p className="review-subtitle">Categorize these transfers to track your spending better</p>
            <div className="review-list">
              {needs_review.map((tx) => (
                <div key={tx.id} className="review-item">
                  <div className="review-info">
                    <span className="review-desc">{tx.description}</span>
                    <span className="review-amount">-{formatCurrency(tx.amount)}</span>
                  </div>
                  <span className="review-date">{formatDate(tx.date)} - {tx.bank}</span>
                  {loggedTx[tx.id] ? (
                    <div className="category-logged">
                      <i className="fa-solid fa-circle-check"></i>
                      Logged as {loggedTx[tx.id]}
                    </div>
                  ) : (
                    <div className="category-buttons">
                      {(tx.suggested_categories || ['Sales', 'Groceries', 'Food', 'Transport', 'Bills', 'Salaries']).slice(0, 6).map((cat) => (
                        <button
                          key={cat}
                          className={`category-btn ${categorizingId === tx.id ? 'disabled' : ''}`}
                          onClick={() => categorizeTransaction(tx.id, cat)}
                          disabled={categorizingId === tx.id}
                        >
                          {cat}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="card needs-review-card review-col review-empty">
            <div className="card-header">
              <i className="fa-solid fa-circle-check" style={{ color: "#2E7D32" }}></i>
              <h3>Needs Review</h3>
            </div>
            <div className="empty-state">
              <p>You&apos;re all caught up! No transactions need categorizing.</p>
            </div>
          </div>
        )}
      </div>

      {/* Savings Goals — moved to a full-width row beneath bank activity & review */}
      <div className="card goals-card goals-row">
        <div className="card-header">
          <i className="fa-solid fa-bullseye" style={{ color: "#E63946" }}></i>
          <h3>Savings Goals</h3>
        </div>
        {goals.length > 0 ? (
          <div className="goals-list">
            {goals.slice(0, 4).map((goal, index) => (
              <div key={index} className="goal-item">
                <div className="goal-info">
                  <span className="goal-name">{goal.name}</span>
                  <span className="goal-progress-text">
                    {formatCurrency(goal.assigned)} / {formatCurrency(goal.target)}
                  </span>
                </div>
                <ProgressBar value={goal.progress} />
              </div>
            ))}
          </div>
        ) : (
          <div className="empty-state">
            <p>No savings goals set yet.</p>
            <button onClick={() => handleNavigation("/goals")} className="action-btn">
              Set Goals
            </button>
          </div>
        )}
      </div>

      {/* Quick Actions */}
      <div className="quick-actions">
        <button onClick={() => handleNavigation("/chat")} className="quick-action-btn primary">
          <i className="fa-solid fa-comments"></i>
          Ask Loamy AI
        </button>
        <button onClick={handleRefreshData} disabled={refreshing} className="quick-action-btn secondary">
          <i className={`fa-solid fa-refresh ${refreshing ? "fa-spin" : ""}`}></i>
          {refreshing ? "Refreshing..." : "Refresh Data"}
        </button>
      </div>
    </div>
  );
};

export default Dashboard;
