import { useState, useRef, useEffect } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faWandMagicSparkles, faCheck, faPaperPlane } from "@fortawesome/free-solid-svg-icons";

const CREDIT_EXAMPLES = "e.g. repayment from my brother, or pay for the shoes I sold";
const DEBIT_EXAMPLES = "e.g. sugar for the shop, or lunch with friends";

const ExpenseCard = ({ item, busy, onResolve, onReply, onDismiss }) => {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState([]);
  const threadRef = useRef(null);
  const suggestion = item.suggested_category || "";
  const isCredit = item.transaction_type === "credit";

  useEffect(() => {
    if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;
  }, [messages, busy]);

  const send = async (event) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    const earlier = messages.filter((m) => m.from === "user").map((m) => m.text);
    setDraft("");
    setMessages((prev) => [...prev, { from: "user", text }]);
    const answer = await onReply(item.id, text, earlier);
    if (answer) setMessages((prev) => [...prev, { from: "loamy", text: answer }]);
  };

  const onKeyDown = (event) => {
    if (event.key !== "Enter" || event.nativeEvent.isComposing || event.keyCode === 229) return;
    send(event);
  };

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

      <div className="rqt-thread" ref={threadRef} aria-live="polite">
        <div className="rqt-msg is-loamy">
          <span className="rqt-avatar" aria-hidden="true">L</span>
          <div className="rqt-bubble is-loamy">
            {item.nudge ||
              (isCredit
                ? "Money came in. Who or what was it from?"
                : "What was this money spent on?")}
          </div>
        </div>
        {messages.map((m, i) =>
          m.from === "user" ? (
            <div key={i} className="rqt-bubble is-user">{m.text}</div>
          ) : (
            <div key={i} className="rqt-msg is-loamy">
              <span className="rqt-avatar" aria-hidden="true">L</span>
              <div className="rqt-bubble is-loamy">{m.text}</div>
            </div>
          )
        )}
        {busy && (
          <div className="rqt-msg is-loamy">
            <span className="rqt-avatar" aria-hidden="true">L</span>
            <div className="rqt-bubble is-loamy is-typing">Thinking...</div>
          </div>
        )}
      </div>

      <form className="rqt-chat-row" onSubmit={send}>
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          placeholder={isCredit ? CREDIT_EXAMPLES : DEBIT_EXAMPLES}
          aria-label={`Tell Loamy about ${item.vendor || "this transaction"}`}
        />
        <button
          type="submit"
          className="rqt-send"
          disabled={busy || !draft.trim()}
          aria-label="Send"
        >
          <FontAwesomeIcon icon={faPaperPlane} />
        </button>
      </form>

      <button type="button" className="rqt-ghost" onClick={() => onDismiss(item.id)} disabled={busy}>
        Dismiss
      </button>
    </article>
  );
};

export default ExpenseCard;
