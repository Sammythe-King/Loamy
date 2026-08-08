import React, { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
    faPlus, faChevronDown, faChevronRight, faXmark, faPen, faTrash,
    faCheck, faHouse, faComment, faChartPie, faRightFromBracket, faFileInvoiceDollar
} from "@fortawesome/free-solid-svg-icons";
import Logo from "./assets/loamylogo.png";
import "./GoalsPage.css";

const API_URL = "http://127.0.0.1:8000";

const avatarColors = ["#e57373", "#f06292", "#ba68c8", "#9575cd", "#7986cb", "#64b5f6", "#4db6ac", "#81c784"];

const GoalsPage = () => {
    const navigate = useNavigate();
    
    // State
    const [accounts, setAccounts] = useState([]);
    const [goals, setGoals] = useState([]);
    const [bills, setBills] = useState([]);
    const [completedGoals, setCompletedGoals] = useState([]);
    const [completedBills, setCompletedBills] = useState([]);
    const [totalReadyToAssign, setTotalReadyToAssign] = useState(0);
    
    // UI State
    const [activeGoal, setActiveGoal] = useState(null);
    const [showPanel, setShowPanel] = useState(false);
    const [isEditMode, setIsEditMode] = useState(false);
    const [showCompletedGoals, setShowCompletedGoals] = useState(false);
    const [showCompletedBills, setShowCompletedBills] = useState(false);
    
    // Modal State
    const [showAccountModal, setShowAccountModal] = useState(false);
    const [showCategoryModal, setShowCategoryModal] = useState(false);
    const [showDeleteModal, setShowDeleteModal] = useState(false);
    const [accountToEdit, setAccountToEdit] = useState(null);
    const [accountToDelete, setAccountToDelete] = useState(null);
    
    // Form State
    const [editAmount, setEditAmount] = useState("");
    const [editDeadline, setEditDeadline] = useState("Monthly");
    const [editSpecificDate, setEditSpecificDate] = useState("");
    const [assignAmount, setAssignAmount] = useState("");
    
    // Account Form
    const [accNickname, setAccNickname] = useState("");
    const [accBalance, setAccBalance] = useState("");
    const [accType, setAccType] = useState("Checking");
    
    // Category Form
    const [newCategoryName, setNewCategoryName] = useState("");
    const [newCategoryAmount, setNewCategoryAmount] = useState("");
    const [newCategoryType, setNewCategoryType] = useState("goal");

    // Default categories
    const billDefaults = ["Rent", "Utilities", "Insurance", "Music", "TV Streaming"];
    const goalDefaults = ["Annual credit card fees"];

    useEffect(() => {
        const sessionData = localStorage.getItem("loamy_session");
        if (!sessionData) {
            navigate("/");
            return;
        }
        loadData();
    }, [navigate]);

    const getAvatarColor = (name) => {
        const charCode = name.charCodeAt(0);
        return avatarColors[charCode % avatarColors.length];
    };

    const toSentenceCase = (text) => {
        if (!text) return "";
        return text.charAt(0).toUpperCase() + text.slice(1);
    };

    const loadData = async () => {
        try {
            // Fetch Accounts
            let totalBalance = 0;
            try {
                const accRes = await fetch(`${API_URL}/get-accounts`);
                const accData = await accRes.json();
                if (accData.accounts) {
                    setAccounts(accData.accounts);
                    totalBalance = accData.accounts.reduce((sum, acc) => sum + acc.balance, 0);
                }
            } catch (e) {
                console.error("Accounts endpoint not available:", e);
            }

            // Dedupe goals
            try {
                await fetch(`${API_URL}/dedupe-goals`, { method: "POST" });
            } catch (e) { }

            // Fetch Goals
            const response = await fetch(`${API_URL}/get-goals`);
            const data = await response.json();

            let totalAssigned = 0;
            const goalsArr = [];
            const billsArr = [];
            const completedGoalsArr = [];
            const completedBillsArr = [];
            const addedItems = new Set();

            if (data.goals) {
                data.goals.forEach(goal => {
                    totalAssigned += goal.assigned || 0;
                    const itemLower = goal.item.toLowerCase();
                    
                    if (addedItems.has(itemLower)) return;
                    addedItems.add(itemLower);

                    const targetAmount = goal.amount || 0;
                    const assignedAmount = goal.assigned || 0;
                    const isCompleted = targetAmount > 0 && assignedAmount >= targetAmount;
                    const isBill = goal.category === "bill" || billDefaults.some(d => d.toLowerCase() === itemLower);

                    if (isBill) {
                        if (isCompleted) {
                            completedBillsArr.push(goal);
                        } else {
                            billsArr.push(goal);
                        }
                    } else {
                        if (isCompleted) {
                            completedGoalsArr.push(goal);
                        } else {
                            goalsArr.push(goal);
                        }
                    }
                });
            }

            // Add defaults that weren't in database
            billDefaults.forEach(name => {
                if (!addedItems.has(name.toLowerCase())) {
                    billsArr.push({ item: name, amount: 0, assigned: 0, category: "bill" });
                }
            });

            goalDefaults.forEach(name => {
                if (!addedItems.has(name.toLowerCase())) {
                    goalsArr.push({ item: name, amount: 0, assigned: 0, category: "goal" });
                }
            });

            setGoals(goalsArr);
            setBills(billsArr);
            setCompletedGoals(completedGoalsArr);
            setCompletedBills(completedBillsArr);
            setTotalReadyToAssign(totalBalance - totalAssigned);

        } catch (e) {
            console.error("Load error:", e);
        }
    };

    const openPanel = (goal) => {
        setActiveGoal(goal);
        setShowPanel(true);
        setIsEditMode(false);
        setEditAmount(goal.amount?.toString() || "");
        setAssignAmount("");
        
        const deadline = goal.deadline || "Monthly";
        if (/^\d{4}-\d{2}-\d{2}$/.test(deadline)) {
            setEditDeadline("specific-date");
            setEditSpecificDate(deadline);
        } else {
            setEditDeadline("Monthly");
            setEditSpecificDate("");
        }
    };

    const closePanel = () => {
        setShowPanel(false);
        setActiveGoal(null);
        setIsEditMode(false);
    };

    const handleSaveTarget = async () => {
        const newAmount = parseFloat(editAmount) || 0;
        const newDeadline = editDeadline === "specific-date" ? editSpecificDate : "Monthly";

        try {
            const res = await fetch(`${API_URL}/update-goal`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    item: activeGoal.item,
                    amount: newAmount,
                    deadline: newDeadline,
                    category: activeGoal.category || "goal",
                    assigned: activeGoal.assigned || 0
                })
            });

            if (res.ok) {
                const updatedGoal = { ...activeGoal, amount: newAmount, deadline: newDeadline };
                setActiveGoal(updatedGoal);
                setIsEditMode(false);
                loadData();
            }
        } catch (e) {
            console.error("Save failed:", e);
            alert("Connection error. Is the backend running?");
        }
    };

    const handleAssign = async () => {
        if (!activeGoal) return;

        const amountToAssign = parseFloat(assignAmount) || 0;
        const targetAmount = activeGoal.amount || 0;
        const assignedAmount = activeGoal.assigned || 0;
        const remainingAmount = Math.max(0, targetAmount - assignedAmount);

        if (amountToAssign <= 0) {
            alert("Please enter an amount to assign.");
            return;
        }
        if (amountToAssign > remainingAmount) {
            alert(`You cannot assign more than the remaining amount ($${remainingAmount.toLocaleString()}).`);
            return;
        }
        if (amountToAssign > totalReadyToAssign) {
            alert(`You don't have enough funds. Available: $${totalReadyToAssign.toLocaleString()}`);
            return;
        }

        try {
            const res = await fetch(`${API_URL}/assign-to-goal`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    item: activeGoal.item,
                    amount: amountToAssign
                })
            });

            if (res.ok) {
                const data = await res.json();
                const updatedGoal = { ...activeGoal, assigned: data.assigned };
                setActiveGoal(updatedGoal);
                setTotalReadyToAssign(prev => prev - amountToAssign);
                setAssignAmount("");
                loadData();
            }
        } catch (e) {
            console.error("Assign failed:", e);
        }
    };

    // Account handlers
    const handleSaveAccount = async () => {
        const balance = parseFloat(accBalance) || 0;
        
        if (!accNickname.trim()) {
            alert("Please enter an account name.");
            return;
        }

        try {
            if (accountToEdit) {
                await fetch(`${API_URL}/update-account/${accountToEdit.id}`, {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        nickname: accNickname,
                        balance: balance,
                        type: accType
                    })
                });
            } else {
                await fetch(`${API_URL}/add-account`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        nickname: accNickname,
                        balance: balance,
                        type: accType
                    })
                });
            }

            setShowAccountModal(false);
            setAccountToEdit(null);
            setAccNickname("");
            setAccBalance("");
            setAccType("Checking");
            loadData();
        } catch (e) {
            console.error("Save account failed:", e);
        }
    };

    const handleDeleteAccount = async () => {
        if (!accountToDelete) return;

        try {
            await fetch(`${API_URL}/delete-account/${accountToDelete}`, {
                method: "DELETE"
            });
            setShowDeleteModal(false);
            setAccountToDelete(null);
            loadData();
        } catch (e) {
            console.error("Delete account failed:", e);
        }
    };

    // Category handlers
    const handleSaveCategory = async () => {
        if (!newCategoryName.trim()) {
            alert("Please enter a name.");
            return;
        }

        try {
            await fetch(`${API_URL}/add-goal`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    item: newCategoryName,
                    amount: parseFloat(newCategoryAmount) || 0,
                    category: newCategoryType,
                    assigned: 0
                })
            });

            setShowCategoryModal(false);
            setNewCategoryName("");
            setNewCategoryAmount("");
            setNewCategoryType("goal");
            loadData();
        } catch (e) {
            console.error("Save category failed:", e);
        }
    };

    const handleLogout = () => {
        if (window.confirm("Are you sure you want to logout?")) {
            localStorage.removeItem("loamy_session");
            navigate("/");
        }
    };

    // Goal row component
    const GoalRow = ({ goal, isCompleted = false }) => {
        const name = toSentenceCase(goal.item);
        const color = getAvatarColor(name);
        const firstLetter = name.charAt(0);
        const amount = goal.amount || 0;
        const assigned = goal.assigned || 0;
        const progress = amount > 0 ? Math.min(100, Math.round((assigned / amount) * 100)) : 0;

        return (
            <div 
                className={`budget-row ${isCompleted ? "completed" : ""} ${activeGoal?.item === goal.item ? "selected" : ""}`}
                onClick={() => openPanel(goal)}
            >
                <div className="row-left">
                    <div className="letter-avatar" style={{ backgroundColor: color }}>
                        {firstLetter}
                    </div>
                    <span className="row-label">{name}</span>
                </div>
                <div className="row-right">
                    <span className="row-target">
                        {amount > 0 ? `$${amount.toLocaleString()}` : "No target"}
                    </span>
                    {amount > 0 && (
                        <div className="mini-progress">
                            <div className="mini-progress-fill" style={{ width: `${progress}%` }}></div>
                        </div>
                    )}
                </div>
            </div>
        );
    };

    // Calculate panel values
    const targetAmount = activeGoal?.amount || 0;
    const assignedAmount = activeGoal?.assigned || 0;
    const remainingAmount = Math.max(0, targetAmount - assignedAmount);
    const progressPercent = targetAmount > 0 ? Math.min(100, Math.round((assignedAmount / targetAmount) * 100)) : 0;
    
    const deadlineDisplay = (() => {
        const deadline = activeGoal?.deadline || "Monthly";
        if (/^\d{4}-\d{2}-\d{2}$/.test(deadline)) {
            return new Date(deadline + "T00:00:00").toLocaleDateString("en-US", {
                year: "numeric",
                month: "short",
                day: "numeric"
            });
        }
        return deadline;
    })();

    return (
        <div className="goals-container">
            {/* Sidebar */}
            <aside className="goals-sidebar">
                <div className="sidebar-header">
                    <img src={Logo} alt="Loamy" className="sidebar-logo" />
                    <span className="logo-text">Loamy</span>
                </div>

                <div className="sidebar-section">
                    <div className="rta-display">
                        <span className="rta-label">Ready to Assign</span>
                        <span className="rta-amount">${totalReadyToAssign.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
                    </div>
                </div>

                <div className="sidebar-section">
                    <div className="section-header">
                        <span>Accounts</span>
                        <button 
                            className="add-btn"
                            onClick={() => {
                                setAccountToEdit(null);
                                setAccNickname("");
                                setAccBalance("");
                                setAccType("Checking");
                                setShowAccountModal(true);
                            }}
                        >
                            <FontAwesomeIcon icon={faPlus} />
                        </button>
                    </div>
                    <div className="accounts-list">
                        {accounts.map(acc => (
                            <div key={acc.id} className="account-item">
                                <span className="account-name">{acc.nickname}</span>
                                <div className="account-right">
                                    <span className="account-balance">${acc.balance.toLocaleString()}</span>
                                    <div className="account-actions">
                                        <button 
                                            className="action-btn"
                                            onClick={() => {
                                                setAccountToEdit(acc);
                                                setAccNickname(acc.nickname);
                                                setAccBalance(acc.balance.toString());
                                                setAccType(acc.type || "Checking");
                                                setShowAccountModal(true);
                                            }}
                                        >
                                            <FontAwesomeIcon icon={faPen} />
                                        </button>
                                        <button 
                                            className="action-btn delete"
                                            onClick={() => {
                                                setAccountToDelete(acc.id);
                                                setShowDeleteModal(true);
                                            }}
                                        >
                                            <FontAwesomeIcon icon={faTrash} />
                                        </button>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>

                <div className="sidebar-nav">
                    <Link to="/chat" className="nav-item">
                        <FontAwesomeIcon icon={faComment} />
                        <span>AI Chat</span>
                    </Link>
                    <Link to="/dashboard" className="nav-item">
                        <FontAwesomeIcon icon={faHouse} />
                        <span>Dashboard</span>
                    </Link>
                    <Link to="/spending" className="nav-item">
                        <FontAwesomeIcon icon={faChartPie} />
                        <span>Spending</span>
                    </Link>
                    <Link to="/invoices" className="nav-item">
                        <FontAwesomeIcon icon={faFileInvoiceDollar} />
                        <span>Invoices</span>
                    </Link>
                    <button className="nav-item logout" onClick={handleLogout}>
                        <FontAwesomeIcon icon={faRightFromBracket} />
                        <span>Logout</span>
                    </button>
                </div>
            </aside>

            {/* Main Content */}
            <main className="goals-main">
                <header className="goals-header">
                    <h1>Budget & Goals</h1>
                    <button 
                        className="add-category-btn"
                        onClick={() => setShowCategoryModal(true)}
                    >
                        <FontAwesomeIcon icon={faPlus} />
                        <span>Add Category</span>
                    </button>
                </header>

                <div className="goals-content">
                    {/* Bills Section */}
                    <section className="category-section">
                        <h2>Monthly Bills</h2>
                        <div className="goals-list">
                            {bills.map((bill, i) => (
                                <GoalRow key={i} goal={bill} />
                            ))}
                        </div>
                        
                        {completedBills.length > 0 && (
                            <div className="completed-section">
                                <button 
                                    className="completed-toggle"
                                    onClick={() => setShowCompletedBills(!showCompletedBills)}
                                >
                                    <FontAwesomeIcon icon={showCompletedBills ? faChevronDown : faChevronRight} />
                                    <span>Completed ({completedBills.length})</span>
                                </button>
                                {showCompletedBills && (
                                    <div className="goals-list completed">
                                        {completedBills.map((bill, i) => (
                                            <GoalRow key={i} goal={bill} isCompleted />
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                    </section>

                    {/* Goals Section */}
                    <section className="category-section">
                        <h2>Savings Goals</h2>
                        <div className="goals-list">
                            {goals.map((goal, i) => (
                                <GoalRow key={i} goal={goal} />
                            ))}
                        </div>
                        
                        {completedGoals.length > 0 && (
                            <div className="completed-section">
                                <button 
                                    className="completed-toggle"
                                    onClick={() => setShowCompletedGoals(!showCompletedGoals)}
                                >
                                    <FontAwesomeIcon icon={showCompletedGoals ? faChevronDown : faChevronRight} />
                                    <span>Completed ({completedGoals.length})</span>
                                </button>
                                {showCompletedGoals && (
                                    <div className="goals-list completed">
                                        {completedGoals.map((goal, i) => (
                                            <GoalRow key={i} goal={goal} isCompleted />
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                    </section>
                </div>
            </main>

            {/* Side Panel */}
            {showPanel && activeGoal && (
                <div className="side-panel">
                    <div className="panel-header">
                        <h3>{toSentenceCase(activeGoal.item)}</h3>
                        <button className="close-btn" onClick={closePanel}>
                            <FontAwesomeIcon icon={faXmark} />
                        </button>
                    </div>

                    {!isEditMode ? (
                        <div className="panel-summary">
                            {/* Progress Circle */}
                            <div className="progress-circle-container">
                                <svg className="progress-circle" viewBox="0 0 100 100">
                                    <circle className="progress-bg" cx="50" cy="50" r="45" />
                                    <circle 
                                        className="progress-fill" 
                                        cx="50" cy="50" r="45"
                                        style={{
                                            strokeDasharray: 283,
                                            strokeDashoffset: 283 - (283 * progressPercent / 100)
                                        }}
                                    />
                                </svg>
                                <span className="progress-text">{progressPercent}%</span>
                            </div>

                            <div className="panel-stats">
                                <div className="stat">
                                    <span className="stat-label">Target</span>
                                    <span className="stat-value">${targetAmount.toLocaleString()}</span>
                                </div>
                                <div className="stat">
                                    <span className="stat-label">Saved</span>
                                    <span className="stat-value">${assignedAmount.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
                                </div>
                                <div className="stat">
                                    <span className="stat-label">Remaining</span>
                                    <span className="stat-value">${remainingAmount.toLocaleString()}</span>
                                </div>
                            </div>

                            <p className="assign-hint">
                                Set aside another <mark>${remainingAmount.toLocaleString()}</mark> to meet your target by <strong>{deadlineDisplay}</strong>.
                            </p>

                            <div className="assign-section">
                                <input 
                                    type="number"
                                    placeholder="Amount to assign"
                                    value={assignAmount}
                                    onChange={(e) => setAssignAmount(e.target.value)}
                                />
                                <button className="assign-btn" onClick={handleAssign}>
                                    Assign
                                </button>
                            </div>

                            <button className="edit-target-btn" onClick={() => setIsEditMode(true)}>
                                <FontAwesomeIcon icon={faPen} />
                                <span>Edit Target</span>
                            </button>
                        </div>
                    ) : (
                        <div className="panel-edit">
                            <div className="form-group">
                                <label>Target Amount</label>
                                <input 
                                    type="number"
                                    value={editAmount}
                                    onChange={(e) => setEditAmount(e.target.value)}
                                    placeholder="0.00"
                                />
                            </div>

                            <div className="form-group">
                                <label>Deadline</label>
                                <select 
                                    value={editDeadline}
                                    onChange={(e) => setEditDeadline(e.target.value)}
                                >
                                    <option value="Monthly">End of Month</option>
                                    <option value="specific-date">Specific Date</option>
                                </select>
                            </div>

                            {editDeadline === "specific-date" && (
                                <div className="form-group">
                                    <label>Date</label>
                                    <input 
                                        type="date"
                                        value={editSpecificDate}
                                        onChange={(e) => setEditSpecificDate(e.target.value)}
                                    />
                                </div>
                            )}

                            <div className="panel-actions">
                                <button className="cancel-btn" onClick={() => setIsEditMode(false)}>
                                    Cancel
                                </button>
                                <button className="save-btn" onClick={handleSaveTarget}>
                                    Save
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            )}

            {/* Account Modal */}
            {showAccountModal && (
                <div className="modal-overlay" onClick={() => setShowAccountModal(false)}>
                    <div className="modal" onClick={(e) => e.stopPropagation()}>
                        <h3>{accountToEdit ? "Edit Account" : "Add Account"}</h3>
                        <div className="form-group">
                            <label>Account Name</label>
                            <input 
                                type="text"
                                value={accNickname}
                                onChange={(e) => setAccNickname(e.target.value)}
                                placeholder="e.g., Main Checking"
                            />
                        </div>
                        <div className="form-group">
                            <label>Balance</label>
                            <input 
                                type="number"
                                value={accBalance}
                                onChange={(e) => setAccBalance(e.target.value)}
                                placeholder="0.00"
                            />
                        </div>
                        <div className="form-group">
                            <label>Type</label>
                            <select value={accType} onChange={(e) => setAccType(e.target.value)}>
                                <option value="Checking">Checking</option>
                                <option value="Savings">Savings</option>
                                <option value="Credit">Credit Card</option>
                                <option value="Cash">Cash</option>
                            </select>
                        </div>
                        <div className="modal-actions">
                            <button className="cancel-btn" onClick={() => setShowAccountModal(false)}>
                                Cancel
                            </button>
                            <button className="save-btn" onClick={handleSaveAccount}>
                                Save
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Category Modal */}
            {showCategoryModal && (
                <div className="modal-overlay" onClick={() => setShowCategoryModal(false)}>
                    <div className="modal" onClick={(e) => e.stopPropagation()}>
                        <h3>Add Category</h3>
                        <div className="form-group">
                            <label>Name</label>
                            <input 
                                type="text"
                                value={newCategoryName}
                                onChange={(e) => setNewCategoryName(e.target.value)}
                                placeholder="e.g., Vacation Fund"
                            />
                        </div>
                        <div className="form-group">
                            <label>Target Amount</label>
                            <input 
                                type="number"
                                value={newCategoryAmount}
                                onChange={(e) => setNewCategoryAmount(e.target.value)}
                                placeholder="0.00"
                            />
                        </div>
                        <div className="form-group">
                            <label>Type</label>
                            <select value={newCategoryType} onChange={(e) => setNewCategoryType(e.target.value)}>
                                <option value="goal">Savings Goal</option>
                                <option value="bill">Monthly Bill</option>
                            </select>
                        </div>
                        <div className="modal-actions">
                            <button className="cancel-btn" onClick={() => setShowCategoryModal(false)}>
                                Cancel
                            </button>
                            <button className="save-btn" onClick={handleSaveCategory}>
                                Save
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Delete Confirmation Modal */}
            {showDeleteModal && (
                <div className="modal-overlay" onClick={() => setShowDeleteModal(false)}>
                    <div className="modal" onClick={(e) => e.stopPropagation()}>
                        <h3>Delete Account?</h3>
                        <p>This action cannot be undone.</p>
                        <div className="modal-actions">
                            <button className="cancel-btn" onClick={() => setShowDeleteModal(false)}>
                                Cancel
                            </button>
                            <button className="delete-btn" onClick={handleDeleteAccount}>
                                Delete
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default GoalsPage;
