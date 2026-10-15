import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faWandMagicSparkles, faUser } from "@fortawesome/free-solid-svg-icons";

const formatWhen = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
};

const ActivityList = ({ entries, busyId, onUndo }) => (
  <ol className="rqt-timeline">
    {entries.map((entry) => {
      const amount = `\u20A6${Number(entry.amount).toLocaleString()}`;
      return (
        <li key={entry.id} className="rqt-log-row">
          <span className={`rqt-log-icon ${entry.auto ? "is-ai" : ""}`} aria-hidden="true">
            <FontAwesomeIcon icon={entry.auto ? faWandMagicSparkles : faUser} />
          </span>
          <div className="rqt-log-body">
            <p>
              {entry.auto ? "AI auto-linked a " : "You confirmed a "}
              <strong>{amount}</strong> transfer from <strong>{entry.from_bank}</strong> to{" "}
              <strong>{entry.to_bank}</strong>
            </p>
            <span className="rqt-log-time">{formatWhen(entry.resolved_at) || entry.date}</span>
          </div>
          <button
            type="button"
            className="rqt-ghost rqt-undo"
            onClick={() => onUndo(entry.id)}
            disabled={busyId === entry.id}
          >
            Undo
          </button>
        </li>
      );
    })}
  </ol>
);

export default ActivityList;
