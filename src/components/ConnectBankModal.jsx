import { useEffect, useState } from "react";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faXmark, faCheckCircle, faBuildingColumns } from "@fortawesome/free-solid-svg-icons";
import {
  fetchAvailableBanks,
  fetchConnectedAccounts,
  connectBank,
} from "../services/bankService";
import "./ConnectBankModal.css";

/*
 * ConnectBankModal - grid of supported Nigerian banks.
 * Already-connected banks are shown but disabled; picking an unconnected bank
 * reveals an optional account-tail / nickname form, then POSTs to
 * /api/v1/accounts/connect (which also kicks off a background Gmail sync).
 */
export default function ConnectBankModal({ isOpen, onClose, userId, onConnected }) {
  const [banks, setBanks] = useState([]);
  const [connectedSlugs, setConnectedSlugs] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [accountTail, setAccountTail] = useState("");
  const [nickname, setNickname] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(null);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      setSelected(null);
      setSuccess(null);
      const [banksRes, accountsRes] = await Promise.all([
        fetchAvailableBanks(),
        fetchConnectedAccounts(userId),
      ]);
      if (cancelled) return;
      if (banksRes.status !== "success") {
        setError(banksRes.error || "Could not load banks.");
      } else {
        setBanks(banksRes.data?.banks || []);
      }
      const accounts = accountsRes.status === "success" ? accountsRes.data?.accounts || [] : [];
      setConnectedSlugs(new Set(accounts.map((a) => a.bank_slug)));
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [isOpen, userId]);

  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, onClose, submitting]);

  if (!isOpen) return null;

  const handleConnect = async (e) => {
    e.preventDefault();
    if (!selected || submitting) return;
    setSubmitting(true);
    setError(null);
    const res = await connectBank(userId, selected.slug, {
      accountTail: accountTail.trim(),
      nickname: nickname.trim(),
    });
    setSubmitting(false);
    if (res.status !== "success") {
      setError(res.error || "Could not connect this bank.");
      return;
    }
    const account = res.data?.account;
    setConnectedSlugs((prev) => new Set(prev).add(selected.slug));
    setSuccess(selected.name);
    setSelected(null);
    setAccountTail("");
    setNickname("");
    onConnected?.(account);
  };

  return (
    <div className="cbm-overlay" onClick={() => !submitting && onClose()}>
      <div
        className="cbm-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cbm-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="cbm-header">
          <div>
            <h2 id="cbm-title">Connect a bank</h2>
            <p>Loamy reads your bank&apos;s email alerts to track transactions automatically.</p>
          </div>
          <button type="button" className="cbm-close" onClick={onClose} aria-label="Close" disabled={submitting}>
            <FontAwesomeIcon icon={faXmark} />
          </button>
        </header>

        {success && (
          <div className="cbm-success" role="status">
            <FontAwesomeIcon icon={faCheckCircle} />
            <span>{success} connected. Syncing your alerts in the background.</span>
          </div>
        )}
        {error && <div className="cbm-error" role="alert">{error}</div>}

        {loading ? (
          <div className="cbm-grid" aria-busy="true">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="cbm-bank cbm-skeleton" />
            ))}
          </div>
        ) : (
          <ul className="cbm-grid" role="list">
            {banks.map((bank) => {
              const isConnected = connectedSlugs.has(bank.slug);
              const isSelected = selected?.slug === bank.slug;
              return (
                <li key={bank.slug}>
                  <button
                    type="button"
                    className={`cbm-bank${isSelected ? " is-selected" : ""}${isConnected ? " is-connected" : ""}`}
                    onClick={() => !isConnected && setSelected(bank)}
                    disabled={isConnected || submitting}
                    aria-pressed={isSelected}
                  >
                    {bank.logo ? (
                      <img src={bank.logo} alt="" className="cbm-logo" width={40} height={40} />
                    ) : (
                      <span className="cbm-logo cbm-logo-fallback">
                        <FontAwesomeIcon icon={faBuildingColumns} />
                      </span>
                    )}
                    <span className="cbm-bank-name">{bank.name}</span>
                    {isConnected && <span className="cbm-badge">Connected</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        {selected && (
          <form className="cbm-form" onSubmit={handleConnect}>
            <p className="cbm-form-title">Connect {selected.name}</p>
            <div className="cbm-fields">
              <label className="cbm-field">
                <span>Last 4 digits (optional)</span>
                <input
                  type="text"
                  inputMode="numeric"
                  maxLength={4}
                  pattern="[0-9]{0,4}"
                  value={accountTail}
                  onChange={(e) => setAccountTail(e.target.value.replace(/\D/g, "").slice(0, 4))}
                  placeholder="1234"
                />
              </label>
              <label className="cbm-field">
                <span>Nickname (optional)</span>
                <input
                  type="text"
                  maxLength={80}
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  placeholder={`${selected.name} Savings`}
                />
              </label>
            </div>
            <div className="cbm-actions">
              <button type="button" className="cbm-btn-secondary" onClick={() => setSelected(null)} disabled={submitting}>
                Cancel
              </button>
              <button type="submit" className="cbm-btn-primary" disabled={submitting}>
                {submitting ? "Connecting..." : "Connect & sync"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
