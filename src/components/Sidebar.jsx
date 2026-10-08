import { NavLink } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faChartPie, faComment, faFileInvoiceDollar, faListCheck, faBuildingColumns } from "@fortawesome/free-solid-svg-icons";
import "./Sidebar.css";

const links = [
  ["/spending", "Spending Analysis", faChartPie],
  ["/chat", "Chat", faComment],
  ["/invoices", "Invoices", faFileInvoiceDollar],
  ["/accounts", "Bank Accounts", faBuildingColumns],
  ["/review-queue", "Review Queue", faListCheck],
];

export default function Sidebar() {
  return <aside className="sidebar shared-sidebar">
    <div className="sidebar-header"><h2>Loamy</h2></div>
    <div className="sidebar-menu"><div className="menu-section">
      <p className="section-label">Tools</p>
      {links.map(([to, label, icon]) => <NavLink key={to} to={to} className={({ isActive }) => `menu-item${isActive ? " active" : ""}`}>
        <FontAwesomeIcon icon={icon} /><span>{label}</span>
      </NavLink>)}
    </div></div>
  </aside>;
}

