import { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { 
  faChartPie, faComment, faFileInvoiceDollar, faPlus,
  faCheck, faTrash, faClock, faCheckCircle, faListCheck
} from "@fortawesome/free-solid-svg-icons";
import "./InvoicesPage.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

const InvoicesPage = () => {
  const navigate = useNavigate();
  const [invoices, setInvoices] = useState([]);
  const [summary, setSummary] = useState({ total_unpaid: 0, total_paid: 0, unpaid_count: 0, paid_count: 0 });
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [formData, setFormData] = useState({
    client_name: "",
    amount: "",
    description: "",
    due_date: "",
    category: "Freelance"
  });

  useEffect(() => {
    fetchInvoices();
  }, []);

  const fetchInvoices = async () => {
    try {
      setLoading(true);
      const response = await fetch(`${API_URL}/get-invoices`);
      const data = await response.json();
      if (data.status === "success") {
        setInvoices(data.invoices || []);
        setSummary(data.summary || {});
      }
    } catch (err) {
      console.error("Error fetching invoices:", err);
    } finally {
      setLoading(false);
    }
  };

  const createInvoice = async (e) => {
    e.preventDefault();
    try {
      const response = await fetch(`${API_URL}/create-invoice`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...formData,
          amount: parseFloat(formData.amount)
        })
      });
      const data = await response.json();
      if (data.status === "success") {
        setShowForm(false);
        setFormData({ client_name: "", amount: "", description: "", due_date: "", category: "Freelance" });
        fetchInvoices();
      }
    } catch (err) {
      console.error("Error creating invoice:", err);
    }
  };

  const markAsPaid = async (invoiceId) => {
    try {
      const response = await fetch(`${API_URL}/mark-invoice-paid/${invoiceId}`, {
        method: "POST"
      });
      const data = await response.json();
      if (data.status === "success") {
        fetchInvoices();
      }
    } catch (err) {
      console.error("Error marking as paid:", err);
    }
  };

  const deleteInvoice = async (invoiceId) => {
    if (!window.confirm("Are you sure you want to delete this invoice?")) return;
    try {
      const response = await fetch(`${API_URL}/delete-invoice/${invoiceId}`, {
        method: "DELETE"
      });
      const data = await response.json();
      if (data.status === "success") {
        fetchInvoices();
      }
    } catch (err) {
      console.error("Error deleting invoice:", err);
    }
  };

  const formatCurrency = (amount) => {
    return `₦${parseFloat(amount || 0).toLocaleString("en-NG", { minimumFractionDigits: 2 })}`;
  };

  const formatDate = (dateStr) => {
    if (!dateStr) return "";
    const date = new Date(dateStr);
    return date.toLocaleDateString("en-NG", { month: "short", day: "numeric", year: "numeric" });
  };

  const isOverdue = (dueDate) => {
    if (!dueDate) return false;
    return new Date(dueDate) < new Date();
  };

  const unpaidInvoices = invoices.filter(inv => inv.status === "unpaid");
  const paidInvoices = invoices.filter(inv => inv.status === "paid");

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
            <Link to="/invoices" className="menu-item active">
              <FontAwesomeIcon icon={faFileInvoiceDollar} />
              <span>Invoices</span>
            </Link>
            <Link to="/review-queue" className="menu-item">
              <FontAwesomeIcon icon={faListCheck} />
              <span>Review Queue</span>
            </Link>
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="invoices-main">
        <header className="invoices-header">
          <div>
            <h1>Invoices</h1>
            <p>Track money owed to you and payments received</p>
          </div>
          <button className="add-invoice-btn" onClick={() => setShowForm(true)}>
            <FontAwesomeIcon icon={faPlus} />
            New Invoice
          </button>
        </header>

        {/* Summary Cards */}
        <div className="summary-cards">
          <div className="summary-card unpaid">
            <FontAwesomeIcon icon={faClock} className="summary-icon" />
            <div className="summary-info">
              <span className="summary-label">Unpaid</span>
              <span className="summary-amount">{formatCurrency(summary.total_unpaid)}</span>
              <span className="summary-count">{summary.unpaid_count} invoice(s)</span>
            </div>
          </div>
          <div className="summary-card paid">
            <FontAwesomeIcon icon={faCheckCircle} className="summary-icon" />
            <div className="summary-info">
              <span className="summary-label">Paid</span>
              <span className="summary-amount">{formatCurrency(summary.total_paid)}</span>
              <span className="summary-count">{summary.paid_count} invoice(s)</span>
            </div>
          </div>
        </div>

        {/* Invoice Form Modal */}
        {showForm && (
          <div className="modal-overlay" onClick={() => setShowForm(false)}>
            <div className="invoice-form-modal" onClick={e => e.stopPropagation()}>
              <h2>Create New Invoice</h2>
              <form onSubmit={createInvoice}>
                <div className="form-group">
                  <label>Client Name</label>
                  <input
                    type="text"
                    value={formData.client_name}
                    onChange={e => setFormData({...formData, client_name: e.target.value})}
                    placeholder="e.g., Jimmy Ayo"
                    required
                  />
                </div>
                <div className="form-group">
                  <label>Amount (NGN)</label>
                  <input
                    type="number"
                    value={formData.amount}
                    onChange={e => setFormData({...formData, amount: e.target.value})}
                    placeholder="e.g., 50000"
                    required
                  />
                </div>
                <div className="form-group">
                  <label>Description</label>
                  <input
                    type="text"
                    value={formData.description}
                    onChange={e => setFormData({...formData, description: e.target.value})}
                    placeholder="e.g., Website development"
                  />
                </div>
                <div className="form-group">
                  <label>Due Date</label>
                  <input
                    type="date"
                    value={formData.due_date}
                    onChange={e => setFormData({...formData, due_date: e.target.value})}
                  />
                </div>
                <div className="form-group">
                  <label>Category</label>
                  <select
                    value={formData.category}
                    onChange={e => setFormData({...formData, category: e.target.value})}
                  >
                    <option value="Freelance">Freelance</option>
                    <option value="Services">Services</option>
                    <option value="Products">Products</option>
                    <option value="Rent">Rent</option>
                    <option value="Loan">Loan</option>
                    <option value="Other">Other</option>
                  </select>
                </div>
                <div className="form-actions">
                  <button type="button" className="cancel-btn" onClick={() => setShowForm(false)}>
                    Cancel
                  </button>
                  <button type="submit" className="submit-btn">
                    Create Invoice
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}

        {/* Unpaid Invoices */}
        <section className="invoices-section">
          <h2>Unpaid ({unpaidInvoices.length})</h2>
          {loading ? (
            <div className="loading">Loading invoices...</div>
          ) : unpaidInvoices.length === 0 ? (
            <div className="empty-state">
              <p>No unpaid invoices. Great job collecting payments!</p>
            </div>
          ) : (
            <div className="invoices-list">
              {unpaidInvoices.map(invoice => (
                <div key={invoice.id} className={`invoice-card ${isOverdue(invoice.due_date) ? 'overdue' : ''}`}>
                  <div className="invoice-main">
                    <div className="invoice-client">{invoice.client_name}</div>
                    <div className="invoice-amount">{formatCurrency(invoice.amount)}</div>
                  </div>
                  <div className="invoice-details">
                    <span className="invoice-desc">{invoice.description || "No description"}</span>
                    <span className="invoice-meta">
                      {invoice.due_date && (
                        <>Due: {formatDate(invoice.due_date)} {isOverdue(invoice.due_date) && <span className="overdue-badge">Overdue</span>}</>
                      )}
                    </span>
                  </div>
                  <div className="invoice-actions">
                    <button className="paid-btn" onClick={() => markAsPaid(invoice.id)}>
                      <FontAwesomeIcon icon={faCheck} /> Mark Paid
                    </button>
                    <button className="delete-btn" onClick={() => deleteInvoice(invoice.id)}>
                      <FontAwesomeIcon icon={faTrash} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Paid Invoices */}
        {paidInvoices.length > 0 && (
          <section className="invoices-section paid-section">
            <h2>Paid ({paidInvoices.length})</h2>
            <div className="invoices-list">
              {paidInvoices.map(invoice => (
                <div key={invoice.id} className="invoice-card paid">
                  <div className="invoice-main">
                    <div className="invoice-client">{invoice.client_name}</div>
                    <div className="invoice-amount">{formatCurrency(invoice.amount)}</div>
                  </div>
                  <div className="invoice-details">
                    <span className="invoice-desc">{invoice.description || "No description"}</span>
                    <span className="invoice-meta">Paid on {formatDate(invoice.paid_date)}</span>
                  </div>
                  <div className="invoice-actions">
                    <span className="paid-badge">
                      <FontAwesomeIcon icon={faCheckCircle} /> Paid
                    </span>
                    <button className="delete-btn" onClick={() => deleteInvoice(invoice.id)}>
                      <FontAwesomeIcon icon={faTrash} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
      </main>
    </div>
  );
};

export default InvoicesPage;
