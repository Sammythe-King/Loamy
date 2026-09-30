import React, { useEffect, useState } from "react";
import { isRealUserId } from "./services/activitySyncService";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

const SOURCE_META = {
  manual_cash: { icon: "fa-brands fa-whatsapp", label: "WhatsApp cash" },
  receipt: { icon: "fa-solid fa-receipt", label: "Receipt" },
};

function formatMoney(amount, currency) {
  const value = Number(amount || 0).toLocaleString("en-NG", { maximumFractionDigits: 2 });
  return currency === "NGN" ? `\u20a6${value}` : `${value} ${currency}`;
}

// "1,000 MUR (~\u20a628,000)" for foreign entries, plain "\u20a628,000" otherwise.
function formatDualCurrency(tx, baseCurrency) {
  const base = formatMoney(tx.amount, baseCurrency);
  const original = tx.original_currency;
  if (original && original !== baseCurrency && tx.original_amount) {
    return `${formatMoney(tx.original_amount, original)} (~${base})`;
  }
  return base;
}

function formatShortDate(date) {
  if (!date) return "";
  const d = new Date(date);
  if (Number.isNaN(d.getTime())) return date;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export default function LedgerLogWidget({ userId }) {
  const [items, setItems] = useState([]);
  const [baseCurrency, setBaseCurrency] = useState("NGN");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isRealUserId(userId)) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    fetch(`${API_URL}/ledger-log/${encodeURIComponent(userId)}?limit=6`)
      .then((r) => r.json())
      .then((res) => {
        if (cancelled || res.status !== "success") return;
        setItems(res.data?.items || []);
        setBaseCurrency(res.data?.base_currency || "NGN");
      })
      .catch(() => {})
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [userId]);

  return (
    <div className="card transactions-card ledger-log-card">
      <div className="card-header">
        <i className="fa-brands fa-whatsapp" style={{ color: "#25D366" }} aria-hidden="true"></i>
        <h3>Cash &amp; Receipts Log</h3>
      </div>

      {loading ? (
        <p className="ledger-log-muted">Loading entries...</p>
      ) : items.length === 0 ? (
        <div className="empty-state">
          <p>No cash or receipt entries yet.</p>
          <p className="ledger-log-muted">Text Loamy on WhatsApp, e.g. &quot;Spent 1,000 MUR on lunch&quot;.</p>
        </div>
      ) : (
        <ul className="transactions-list ledger-log-list">
          {items.map((tx) => {
            const meta = SOURCE_META[tx.source] || SOURCE_META.receipt;
            const needsReview = tx.category === "Uncategorized";
            return (
              <li key={tx.id} className="transaction-item">
                <div className="ledger-log-left">
                  <span className="ledger-log-icon" title={meta.label}>
                    <i className={meta.icon} aria-hidden="true"></i>
                    <span className="sr-only">{meta.label}</span>
                  </span>
                  <div className="transaction-info">
                    <span className="transaction-desc">{(tx.description || "Expense").substring(0, 25)}</span>
                    <span className="transaction-date">
                      {formatShortDate(tx.date)} - {needsReview ? (
                        <span className="ledger-log-review">Needs review</span>
                      ) : (
                        tx.category
                      )}
                    </span>
                  </div>
                </div>
                <span className={`transaction-amount ${tx.type === "credit" ? "green" : "red"}`}>
                  {tx.type === "credit" ? "+" : "-"}
                  {formatDualCurrency(tx, baseCurrency)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
