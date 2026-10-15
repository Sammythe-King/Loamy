import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faArrowRight, faBuildingColumns } from "@fortawesome/free-solid-svg-icons";

const BankNode = ({ label, children }) => (
  <div className="rqt-bank">
    <span className="rqt-bank-icon" aria-hidden="true">
      <FontAwesomeIcon icon={faBuildingColumns} />
    </span>
    {children || <span className="rqt-bank-name" title={label}>{label}</span>}
  </div>
);

const TransferCard = ({
  transfer, accounts, destination, busy,
  onDestinationChange, onConfirm, onLink, onReject,
}) => {
  const isPair = transfer.transfer_stage === "candidate";
  const isDebit = transfer.transaction_type !== "credit";
  const thisBank = transfer.bank_name || "Your account";

  const picker = (
    <select
      className="rqt-bank-select"
      value={destination || ""}
      onChange={(e) => onDestinationChange(transfer.id, e.target.value)}
      disabled={busy}
      aria-label={isDebit ? "Destination account" : "Source account"}
    >
      <option value="">{isDebit ? "Select destination..." : "Select source..."}</option>
      {accounts.map((a) => (
        <option key={a.id} value={a.name}>{a.name}</option>
      ))}
      <option value="Another account of mine">Another account of mine</option>
    </select>
  );

  const otherSide = isPair ? <BankNode label={transfer.counterpart_bank || "Another account"} /> : <BankNode>{picker}</BankNode>;
  const thisSide = <BankNode label={thisBank} />;
  const from = isDebit ? thisSide : otherSide;
  const to = isDebit ? otherSide : thisSide;

  return (
    <article className="rqt-card rqt-transfer">
      <div className="rqt-transfer-flow">
        {from}
        <div className="rqt-transfer-mid">
          <span className="rqt-transfer-amount">&#8358;{Number(transfer.amount).toLocaleString()}</span>
          <span className="rqt-transfer-arrow" aria-hidden="true">
            <FontAwesomeIcon icon={faArrowRight} />
          </span>
          <span className="rqt-card-date">{transfer.date || ""}</span>
        </div>
        {to}
      </div>

      <div className="rqt-transfer-actions">
        {isPair ? (
          <button type="button" className="rqt-primary" onClick={() => onConfirm(transfer.id)} disabled={busy}>
            {busy ? <span className="rq-spinner" /> : "Confirm Transfer Pair"}
          </button>
        ) : (
          <button
            type="button"
            className="rqt-primary"
            onClick={() => onLink(transfer.id)}
            disabled={busy || !destination}
          >
            {busy ? <span className="rq-spinner" /> : "Link Account"}
          </button>
        )}
        <button type="button" className="rqt-ghost" onClick={() => onReject(transfer.id)} disabled={busy}>
          Not a transfer
        </button>
      </div>
    </article>
  );
};

export default TransferCard;
