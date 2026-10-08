import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  faChartPie, faComment, faFileInvoiceDollar, faListCheck,
  faArrowLeft, faArrowDown, faArrowUp, faBuildingColumns, faPlus,
} from "@fortawesome/free-solid-svg-icons";
import ConnectBankModal from "../components/ConnectBankModal.jsx";
import {
  fetchAccountTransactions,
  formatNaira,
  getSessionUserId,
} from "../services/bankService";
import "../InvoicesPage.css";
import "./AccountDetail.css";

const TYPE_FILTERS = [
  { value: "", label: "All" },
  { value: "credit", label: "Income" },
  { value: "debit", label: "Expenses" },
];

function formatDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-NG", { day: "numeric", month: "short", year: "numeric" });
}

/*
 * AccountDetail - dashboard for one connected bank.
 * Header, total balance, income/expense metrics and an itemized table, all
 * sourced from GET /api/v1/accounts/{account_id}/transactions.
 */
export default function AccountDetail() {
  const { accountId } = useParams();
  const [userId] = useState(getSessionUserId);
  const [typeFilter, setTypeFilter] = useState("");
  const [result, setResult] = useState({ key: null, data: null, error: null });
  const [modalOpen, setModalOpen] = useState(false);

  // Loading is derived from whether the last response matches the current
  // request, so the effect only sets state from the async callback.
  const requestKey = `${accountId}|${typeFilter}`;
  const loading = result.key !== requestKey;
  const { data, error } = result;

  useEffect(() => {
    let cancelled = false;
    fetchAccountTransactions(userId, accountId, { type: typeFilter, limit: 200 }).then((res) => {
      if (cancelled) return;
      setResult((prev) =>
        res.status === "success"
          ? { key: requestKey, data: res.data, error: null }
          : {
              key: requestKey,
              data: prev.data,
              error: res.error === "no_session" ? "Please log in to view this account." : res.error,
            }
      );
    });
    return () => {
      cancelled = true;
    };
  }, [userId, accountId, typeFilter, requestKey]);

  const account = data?.account;
  const analytics = data?.analytics || {};
  const transactions = data?.transactions || [];

  return (
    <div className="invoices-page">
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
            <Link to="/review-queue" className="menu-item">
              <FontAwesomeIcon icon={faListCheck} />
              <span>Review Queue</span>
            </Link>
          </div>
        </div>
      </aside>

      <main className="invoices-main ad-main">
        <Link to="/dashboard" className="ad-back">
          <FontAwesomeIcon icon={faArrowLeft} />
          <span>Back to dashboard</span>
        </Link>

        <header className="ad-header">
          <div className="ad-bank">
            {account?.logo ? (
              <img src={account.logo} alt="" className="ad-logo" width={52} height={52} />
            ) : (
              <span className="ad-logo ad-logo-fallback">
                <FontAwesomeIcon icon={faBuildingColumns} />
              </span>
            )}
            <div>
              <h1 className="ad-title">{account?.nickname || account?.bank_name || "Bank account"}</h1>
              <p className="ad-subtitle">
                {account?.bank_name}
                {account?.account_tail ? ` •••• ${account.account_tail}` : ""}
                {account?.status && <span className="ad-status">{account.status}</span>}
              </p>
            </div>
          </div>
          <button type="button" className="ad-connect-btn" onClick={() => setModalOpen(true)}>
            <FontAwesomeIcon icon={faPlus} />
            <span>Connect bank</span>
          </button>
        </header>

        {error && <div className="ad-error" role="alert">{error}</div>}

        {!error && (
          <>
            <section className="ad-balance" aria-label="Total balance">
              <p className="ad-label">Total balance</p>
              <p className="ad-balance-value">
                {loading && !data ? "…" : formatNaira(analytics.balance)}
              </p>
              <p className="ad-muted">
                {analytics.last_activity
                  ? `Last activity ${formatDate(analytics.last_activity)}`
                  : "Balance updates from your latest bank alert"}
              </p>
            </section>

            <section className="ad-metrics" aria-label="Account metrics">
              <div className="ad-metric">
                <span className="ad-metric-icon ad-income"><FontAwesomeIcon icon={faArrowDown} /></span>
                <div>
                  <p className="ad-label">Income</p>
                  <p className="ad-metric-value ad-income-text">{formatNaira(analytics.total_credits)}</p>
                </div>
              </div>
              <div className="ad-metric">
                <span className="ad-metric-icon ad-expense"><FontAwesomeIcon icon={faArrowUp} /></span>
                <div>
                  <p className="ad-label">Expenses</p>
                  <p className="ad-metric-value ad-expense-text">{formatNaira(analytics.total_debits)}</p>
                </div>
              </div>
              <div className="ad-metric">
                <div>
                  <p className="ad-label">Net flow</p>
                  <p className={`ad-metric-value ${analytics.net_flow < 0 ? "ad-expense-text" : "ad-income-text"}`}>
                    {formatNaira(analytics.net_flow)}
                  </p>
                </div>
              </div>
              <div className="ad-metric">
                <div>
                  <p className="ad-label">Transactions</p>
                  <p className="ad-metric-value">{analytics.transaction_count ?? 0}</p>
                </div>
              </div>
            </section>

            <section className="ad-table-card" aria-labelledby="ad-tx-title">
              <div className="ad-table-head">
                <h2 id="ad-tx-title">Transactions</h2>
                <div className="ad-filters" role="group" aria-label="Filter by type">
                  {TYPE_FILTERS.map((f) => (
                    <button
                      key={f.value || "all"}
                      type="button"
                      className={`ad-filter${typeFilter === f.value ? " is-active" : ""}`}
                      aria-pressed={typeFilter === f.value}
                      onClick={() => setTypeFilter(f.value)}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
              </div>

              {loading ? (
                <p className="ad-muted ad-empty">Loading transactions…</p>
              ) : transactions.length === 0 ? (
                <p className="ad-muted ad-empty">
                  No transactions yet. New alerts from this bank will appear here after the next sync.
                </p>
              ) : (
                <div className="ad-table-wrap">
                  <table className="ad-table">
                    <thead>
                      <tr>
                        <th scope="col">Date</th>
                        <th scope="col">Description</th>
                        <th scope="col">Category</th>
                        <th scope="col" className="ad-num">Amount</th>
                        <th scope="col" className="ad-num">Balance</th>
                      </tr>
                    </thead>
                    <tbody>
                      {transactions.map((tx) => {
                        const isCredit = tx.type === "credit";
                        return (
                          <tr key={tx.id}>
                            <td className="ad-nowrap">{formatDate(tx.date)}</td>
                            <td className="ad-desc">{tx.merchant || tx.description || tx.raw_subject || "Transaction"}</td>
                            <td><span className="ad-chip">{tx.category || "Uncategorized"}</span></td>
                            <td className={`ad-num ad-nowrap ${isCredit ? "ad-income-text" : "ad-expense-text"}`}>
                              {isCredit ? "+" : "−"}{formatNaira(tx.amount ?? tx.transaction_amount ?? 0)}
                            </td>
                            <td className="ad-num ad-nowrap ad-muted">{formatNaira(tx.balance_after ?? tx.balance ?? tx.running_balance ?? 0)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </>
        )}
      </main>

      <ConnectBankModal
        isOpen={modalOpen}
        onClose={() => setModalOpen(false)}
        userId={userId}
      />
    </div>
  );
}
