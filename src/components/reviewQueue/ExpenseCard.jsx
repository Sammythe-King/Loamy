import { useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faWandMagicSparkles, faCheck } from "@fortawesome/free-solid-svg-icons";

export const EXPENSE_CATEGORIES = [
  "Groceries",
  "Dining & Food",
  "Transportation",
  "Utilities & Bills",
  "Entertainment & Subscriptions",
  "Software & Subscriptions",
  "Business Tools",
  "Shopping",
  "Bank Charges",
  "Personal Expenses",
  "Transfers & Cash",
];

const ExpenseCard = ({ item, busy, onResolve, onDismiss }) => {
  const [choice, setChoice] = useState("");
  const suggestion = item.suggested_category || "";
  const options = suggestion && !EXPENSE_CATEGORIES.includes(suggestion)
    ? [suggestion, ...EXPENSE_CATEGORIES]
    : EXPENSE_CATEGORIES;
  const isCredit = item.transaction_type === "credit";

  return (
    <article className="rqt-card">
      <header className="rqt-card-head">
        <div className="rqt-card-title">
          <h3 title={item.vendor}>{item.vendor || "Transaction"}</h3>
          <span className="rqt-card-date">{item.date || "No date"}</span>
        </div>
        <span className={`rqt-amount ${isCredit ? "is-credit" : ""}`}>
          {isCredit ? "+" : ""}&#8358;{Number(item.amount).toLocaleString()}
        </span>
      </header>

      {suggestion && (
        <div className="rqt-suggest">
          <span className="rqt-suggest-label">
            <FontAwesomeIcon icon={faWandMagicSparkles} />
            Suggested: <strong>{suggestion}</strong>
          </span>
          <button
            type="button"
            className="rqt-approve"
            onClick={() => onResolve(item.id, suggestion)}
            disabled={busy}
          >
            {busy ? <span className="rq-spinner" /> : <FontAwesomeIcon icon={faCheck} />}
            Approve
          </button>
        </div>
      )}

      <div className="rqt-select-row">
        <select
          value={choice}
          onChange={(e) => setChoice(e.target.value)}
          disabled={busy}
          aria-label={`Category for ${item.vendor || "transaction"}`}
        >
          <option value="">Choose a category...</option>
          {options.map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <button
          type="button"
          className="rqt-secondary"
          onClick={() => onResolve(item.id, choice)}
          disabled={busy || !choice}
        >
          Save
        </button>
      </div>

      <button type="button" className="rqt-ghost" onClick={() => onDismiss(item.id)} disabled={busy}>
        Dismiss
      </button>
    </article>
  );
};

export default ExpenseCard;
