import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faArrowsRotate, faBuildingColumns, faPlus } from "@fortawesome/free-solid-svg-icons";
import ConnectBankModal from "../components/ConnectBankModal.jsx";
import Sidebar from "../components/Sidebar.jsx";
import { fetchConnectedAccounts, fetchAccountTransactions, formatNaira, getSessionUserId } from "../services/bankService.js";
import "../InvoicesPage.css";
import "./AllAccountsOverview.css";

const valueOf = (tx, key, fallback) => tx?.[key] ?? fallback;
const descriptionOf = (tx) => tx?.merchant || tx?.description || tx?.raw_subject || "Transaction";

export default function AllAccountsOverview() {
  const userId = getSessionUserId();
  const navigate = useNavigate();
  const [accounts, setAccounts] = useState([]);
  const [transactions, setTransactions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    const result = await fetchConnectedAccounts(userId);
    if (result.status !== "success") { setError(result.error); setLoading(false); return; }
    const nextAccounts = result.data?.accounts || [];
    setAccounts(nextAccounts);
    const rows = await Promise.all(nextAccounts.map((account) => fetchAccountTransactions(userId, account.id, { limit: 100 })));
    setTransactions(rows.flatMap((row, index) => row.status === "success" ? (row.data?.transactions || []).map((tx) => ({ ...tx, bank: nextAccounts[index] })) : []));
    setError("");
    setLoading(false);
  }, [userId]);

  useEffect(() => { load(); }, [load]);

  const metrics = useMemo(() => ({
    balance: accounts.reduce((sum, account) => sum + Number(account.summary?.balance || 0), 0),
    credits: accounts.reduce((sum, account) => sum + Number(account.summary?.total_credits || 0), 0),
    debits: accounts.reduce((sum, account) => sum + Number(account.summary?.total_debits || 0), 0),
  }), [accounts]);

  const syncAll = async () => {
    setSyncing(true);
    window.dispatchEvent(new CustomEvent("loamy:data-synced"));
    window.setTimeout(async () => { await load(); setSyncing(false); }, 1200);
  };

  return <div className="invoices-page all-accounts-page">
    <Sidebar />
    <main className="invoices-main all-accounts-main">
      <header className="all-accounts-header">
        <div><Link className="all-accounts-back" to="/dashboard">Back to dashboard</Link><h1>Bank Accounts Overview</h1><p>All your connected bank activity in one place.</p></div>
        <div className="all-accounts-actions"><button className="secondary-action" onClick={syncAll} disabled={syncing}><FontAwesomeIcon icon={faArrowsRotate} spin={syncing} /> Sync All</button><button className="primary-action" onClick={() => setModalOpen(true)}><FontAwesomeIcon icon={faPlus} /> Connect Bank</button></div>
      </header>
      {error && <div className="accounts-error" role="alert">{error}</div>}
      <section className="accounts-metrics">
        <div><span>Total combined balance</span><strong>{formatNaira(metrics.balance)}</strong></div>
        <div><span>Monthly credits</span><strong className="accounts-positive">{formatNaira(metrics.credits)}</strong></div>
        <div><span>Monthly debits</span><strong className="accounts-negative">{formatNaira(metrics.debits)}</strong></div>
      </section>
      <section><div className="accounts-section-title"><h2>Connected banks</h2><span>{accounts.length} connected</span></div>
        {loading ? <p className="accounts-muted">Loading accounts…</p> : accounts.length === 0 ? <div className="accounts-empty"><FontAwesomeIcon icon={faBuildingColumns} /><p>No bank accounts connected yet.</p><button className="primary-action" onClick={() => setModalOpen(true)}>Connect your first bank</button></div> : <div className="accounts-grid">{accounts.map((account) => <button className="account-overview-card" key={account.id} onClick={() => navigate(`/accounts/${encodeURIComponent(account.id)}`)}><span className="account-logo">{account.logo ? <img src={account.logo} alt="" /> : <FontAwesomeIcon icon={faBuildingColumns} />}</span><span><strong>{account.nickname || account.bank_name}</strong><small>{account.account_tail ? `•••• ${account.account_tail}` : account.bank_name}</small></span><span className="account-card-balance"><strong>{formatNaira(account.summary?.balance || 0)}</strong><small>{account.summary?.transaction_count || 0} transactions</small></span><span className="account-chevron">›</span></button>)}</div>}
      </section>
      <section className="accounts-stream"><div className="accounts-section-title"><h2>All transactions</h2><span>Latest activity across connected banks</span></div>{transactions.length === 0 ? <p className="accounts-muted">No transactions yet.</p> : <div className="accounts-table-wrap"><table><thead><tr><th>Date</th><th>Bank</th><th>Description</th><th>Category</th><th className="accounts-number">Amount</th></tr></thead><tbody>{transactions.sort((a, b) => String(b.date || "").localeCompare(String(a.date || ""))).map((tx, index) => { const credit = tx.type === "credit"; return <tr key={tx.id || `${tx.date}-${index}`}><td>{tx.date || "—"}</td><td><span className="bank-badge">{tx.bank.logo && <img src={tx.bank.logo} alt="" />}{tx.bank.bank_name}</span></td><td>{descriptionOf(tx)}</td><td><span className="category-badge">{tx.category || "Uncategorized"}</span></td><td className={`accounts-number ${credit ? "accounts-positive" : "accounts-negative"}`}>{credit ? "+" : "−"}{formatNaira(valueOf(tx, "amount", tx.transaction_amount || 0))}</td></tr>; })}</tbody></table></div>}</section>
    </main>
    <ConnectBankModal isOpen={modalOpen} onClose={() => setModalOpen(false)} userId={userId} connectedAccounts={accounts} onConnected={() => { setModalOpen(false); load(); }} />
  </div>;
}
