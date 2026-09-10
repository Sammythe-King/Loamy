/**
 * Fintech AI - Savings & Goals Logic
 * Full implementation including Ready to Assign and Sidebar Management
 */

// Resolve the logged-in user so every request is scoped to their account.
// Reads the same session app.js writes at login (Supabase-backed multi-user).
function getLoamyUserId() {
    try {
        var s = JSON.parse(localStorage.getItem('fintech_session') || '{}');
        return s.user_id || 'default';
    } catch (e) {
        return 'default';
    }
}

// 1. Core State
let activeGoal = null;
let accountToDelete = null;
let accountToEdit = null;
let totalReadyToAssign = 0;
const avatarColors = ["#e57373", "#f06292", "#ba68c8", "#9575cd", "#7986cb", "#64b5f6", "#4db6ac", "#81c784"];

const iconMap = {
    "rent": "images/house.png",
    "tv streaming": "images/tv.png",
    "utilities": "images/bolt.png",
    "insurance": "images/insurance.png",
    "music": "images/music.png"
};

// 2. Element Selection
const listBills = document.getElementById('list-bills');
const listGoals = document.getElementById('list-goals');
const sidePanel = document.getElementById('side-panel');
const summaryView = document.getElementById('panel-summary-view');
const editView = document.getElementById('panel-edit-view');
const closePanelBtn = document.getElementById('close-panel');
const editTargetBtn = document.getElementById('trigger-edit-target');
const cancelEditBtn = document.getElementById('cancel-edit');
const saveTargetBtn = document.getElementById('save-target');

// Account Modal Elements
const accountModal = document.getElementById('account-modal');
const deleteModal = document.getElementById('delete-confirm-modal');
const sidebarAccounts = document.getElementById('sidebar-accounts');

// Category Modal Elements
const modal = document.getElementById('bill-modal');
const triggerAddBtn = document.getElementById('trigger-add-bill');
const cancelBillBtn = document.getElementById('cancel-bill');
const saveBillBtn = document.getElementById('save-bill');
const inputName = document.getElementById('new-bill-name');
const inputBillAmount = document.getElementById('new-bill-amount');

// 3. Helper Functions
function getAvatarColor(name) {
    const charCode = name.charCodeAt(0);
    return avatarColors[charCode % avatarColors.length];
}

function toSentenceCase(text) {
    if (!text) return "";
    return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Updates the "Ready to Assign" display in the header
 */
function updateReadyToAssign() {
    const rtaElement = document.getElementById('rta-amount');
    if (rtaElement) {
        rtaElement.textContent = `$${totalReadyToAssign.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }
}

/**
 * Creates an account item with hover edit/delete buttons
 * On hover: balance hides, edit/delete icons appear
 */
function createAccountItem(acc) {
    const div = document.createElement('div');
    div.className = 'account-item';
    div.innerHTML = `
        <span class="account-name">${acc.nickname}</span>
        <div class="account-right">
            <span class="account-balance">$${acc.balance.toLocaleString()}</span>
            <div class="account-actions">
                <svg class="action-icon edit-acc-btn" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>
                    <path d="m15 5 4 4"/>
                </svg>
                <svg class="action-icon delete-acc-btn" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M3 6h18"/>
                    <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/>
                    <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>
                    <line x1="10" x2="10" y1="11" y2="17"/>
                    <line x1="14" x2="14" y1="11" y2="17"/>
                </svg>
            </div>
        </div>
    `;

    // Edit Button Event
    const editBtn = div.querySelector('.edit-acc-btn');
    editBtn.onclick = (e) => {
        e.stopPropagation();
        accountToEdit = acc;
        document.getElementById('acc-nickname').value = acc.nickname;
        document.getElementById('acc-balance').value = acc.balance;
        document.getElementById('acc-type').value = acc.type || 'Checking';
        accountModal.style.display = 'flex';
    };

    // Delete Button Event
    const delBtn = div.querySelector('.delete-acc-btn');
    delBtn.onclick = (e) => {
        e.stopPropagation();
        accountToDelete = acc.id;
        deleteModal.style.display = 'flex';
    };

    return div;
}

/**
 * Renders a row and attaches the click event for the side panel
 * @param {object} goal - The goal object
 * @param {boolean} isCompleted - Whether this goal is completed
 */
function createRow(goal, isCompleted = false) {
    const name = toSentenceCase(goal.item); // Force sentence case
    const amount = goal.amount || 0;
    const lowerName = name.toLowerCase();
    
    const row = document.createElement('div');
    row.className = isCompleted ? 'budget-row completed' : 'budget-row';
    
    const color = getAvatarColor(name);
    const firstLetter = name.charAt(0);
    const hasIcon = iconMap[lowerName];

    row.innerHTML = `
        <div class="row-left">
            <div class="icon-box">
                <div class="letter-avatar" style="background-color: ${color}">${firstLetter}</div>
                ${hasIcon ? `<img src="${iconMap[lowerName]}" class="icon-img" onerror="this.style.display='none'">` : ''}
            </div>
            <span class="row-label">${name}</span>
        </div>
        <div class="row-target">${amount > 0 ? '$' + amount.toLocaleString() : 'No target'}</div>
    `;

    row.addEventListener('click', () => {
        document.querySelectorAll('.budget-row').forEach(r => r.classList.remove('selected'));
        row.classList.add('selected');
        openPanel(goal);
    });

    return row;
}

/**
 * Fetches accounts and goals from the backend and renders them
 */
async function loadData() {
    try {
        // 1. Fetch Accounts
        var totalBalance = 0;
        try {
            const accRes = await fetch('http://127.0.0.1:8000/get-accounts?user_id=' + encodeURIComponent(getLoamyUserId()));
            const accData = await accRes.json();
            
            sidebarAccounts.innerHTML = '';
            
            if (accData.accounts && accData.accounts.length > 0) {
                accData.accounts.forEach(acc => {
                    const accountElement = createAccountItem(acc);
                    sidebarAccounts.appendChild(accountElement);
                    totalBalance += acc.balance;
                });
            }
        } catch (accErr) {
            console.log("Accounts endpoint not available:", accErr);
        }

        // 2. Clean up any duplicate goals/bills
        try {
            await fetch('http://127.0.0.1:8000/dedupe-goals', { method: 'POST' });
        } catch (e) { /* ignore */ }
        
        // 3. Fetch and render Goals
        const response = await fetch('http://127.0.0.1:8000/get-goals?user_id=' + encodeURIComponent(getLoamyUserId()));
        const data = await response.json();
        
        // Calculate total assigned across all goals
        var totalAssigned = 0;
        if (data.goals) {
            data.goals.forEach(goal => {
                console.log("[v0] Goal:", goal.item, "Assigned:", goal.assigned);
                totalAssigned += (goal.assigned || 0);
            });
        }
        
        // Ready to Assign = Total Account Balance - Total Assigned to Goals
        console.log("[v0] Total Balance:", totalBalance);
        console.log("[v0] Total Assigned:", totalAssigned);
        console.log("[v0] Ready to Assign:", totalBalance - totalAssigned);
        
        totalReadyToAssign = totalBalance - totalAssigned;
        updateReadyToAssign();
        
        listBills.innerHTML = '';
        listGoals.innerHTML = '';
        
        // Get completed sections
        var listCompletedGoals = document.getElementById('list-completed-goals');
        var completedGoalsSection = document.getElementById('completed-goals-section');
        var listCompletedBills = document.getElementById('list-completed-bills');
        var completedBillsSection = document.getElementById('completed-bills-section');
        listCompletedGoals.innerHTML = '';
        listCompletedBills.innerHTML = '';
        
        // Define standard categories (these show even if not in database)
        const billDefaults = ["Rent", "Utilities", "Insurance", "Music", "TV Streaming"];
        const goalDefaults = ["Annual credit card fees"];
        
        // Track which items we've already added (to prevent duplicates)
        var addedBills = [];
        var addedGoals = [];
        var addedCompletedGoals = [];
        var addedCompletedBills = [];
        var hasCompletedGoals = false;
        var hasCompletedBills = false;
        
        // 1. First add all items from the database to their correct category
        // Only show one entry per unique item name (skip duplicates)
        if (data.goals) {
            data.goals.forEach(goal => {
                var itemLower = goal.item.toLowerCase();
                var targetAmount = goal.amount || 0;
                var assignedAmount = goal.assigned || 0;
                
                // Check if completed (assigned >= target and target > 0)
                var isCompleted = targetAmount > 0 && assignedAmount >= targetAmount;
                
                // Check if this item is a bill
                var isBill = goal.category === "bill" || billDefaults.some(d => d.toLowerCase() === itemLower);
                
                if (isBill) {
                    // Skip duplicates
                    if (addedBills.includes(itemLower) || addedCompletedBills.includes(itemLower)) return;
                    
                    if (isCompleted) {
                        // Completed bill
                        listCompletedBills.appendChild(createRow(goal, true));
                        addedCompletedBills.push(itemLower);
                        hasCompletedBills = true;
                    } else {
                        // Active bill
                        listBills.appendChild(createRow(goal, false));
                        addedBills.push(itemLower);
                    }
                } else {
                    // It's a goal
                    // Skip duplicates
                    if (addedGoals.includes(itemLower) || addedCompletedGoals.includes(itemLower)) return;
                    
                    if (isCompleted) {
                        // Completed goal
                        listCompletedGoals.appendChild(createRow(goal, true));
                        addedCompletedGoals.push(itemLower);
                        hasCompletedGoals = true;
                    } else {
                        // Active goal
                        listGoals.appendChild(createRow(goal, false));
                        addedGoals.push(itemLower);
                    }
                }
            });
        }
        
        // Show/hide completed sections
        completedGoalsSection.style.display = hasCompletedGoals ? 'block' : 'none';
        completedBillsSection.style.display = hasCompletedBills ? 'block' : 'none';
        
        // 2. Add any default bills that weren't in the database
        billDefaults.forEach(name => {
            if (!addedBills.includes(name.toLowerCase())) {
                listBills.appendChild(createRow({ item: name, amount: 0 }, false));
            }
        });
        
        // 3. Add any default goals that weren't in the database
        goalDefaults.forEach(name => {
            if (!addedGoals.includes(name.toLowerCase())) {
                listGoals.appendChild(createRow({ item: name, amount: 0 }, false));
            }
        });

        lucide.createIcons();
    } catch (e) {
        console.error("Load error:", e);
    }
}

/**
 * Side Panel Controls
 */
function openPanel(goal) {
    activeGoal = goal;
    sidePanel.classList.add('active');
    summaryView.style.display = 'block';
    editView.style.display = 'none';

    // Get values
    var targetAmount = goal.amount || 0;
    var assignedAmount = goal.assigned || 0;
    var remainingAmount = Math.max(0, targetAmount - assignedAmount);
    
    // Calculate progress percentage
    var progressPercent = 0;
    if (targetAmount > 0) {
        progressPercent = Math.min(100, Math.round((assignedAmount / targetAmount) * 100));
    }

    // Format the deadline for display
    var deadline = goal.deadline || "Monthly";
    var deadlineDisplay = deadline;
    
    // Check if it's a specific date (YYYY-MM-DD format)
    if (/^\d{4}-\d{2}-\d{2}$/.test(deadline)) {
        var dateObj = new Date(deadline + "T00:00:00");
        deadlineDisplay = dateObj.toLocaleDateString('en-US', { 
            year: 'numeric', 
            month: 'short', 
            day: 'numeric' 
        });
    }

    // Update panel text
    document.getElementById('panel-title-text').querySelector('span').textContent = toSentenceCase(goal.item);
    document.getElementById('stat-target').textContent = `$${targetAmount.toLocaleString()}`;
    document.getElementById('stat-saved').textContent = `$${assignedAmount.toLocaleString(undefined, { minimumFractionDigits: 2 })}`;
    document.getElementById('stat-remaining').textContent = `$${remainingAmount.toLocaleString()}`;
    
    // Update the hint text to include deadline
    var hintElement = document.getElementById('assign-hint');
    hintElement.innerHTML = `Set aside another <mark>$${remainingAmount.toLocaleString()}</mark> to meet your target by <strong>${deadlineDisplay}</strong>.`;
    
    // Update progress circle (circumference is 283 for r=45)
    var circumference = 283;
    var offset = circumference - (circumference * progressPercent / 100);
    var circle = document.getElementById('progress-circle');
    circle.style.strokeDashoffset = offset;
    document.getElementById('progress-percent').textContent = progressPercent + "%";
}

if (closePanelBtn) {
    closePanelBtn.onclick = () => {
        sidePanel.classList.remove('active');
        document.querySelectorAll('.budget-row').forEach(r => r.classList.remove('selected'));
    };
}

if (editTargetBtn) {
    editTargetBtn.onclick = () => {
        summaryView.style.display = 'none';
        editView.style.display = 'block';
        document.getElementById('edit-amount').value = activeGoal.amount || "";
        
        // Populate deadline fields
        var deadlineType = document.getElementById('edit-deadline-type');
        var specificDateBlock = document.getElementById('specific-date-block');
        var specificDateInput = document.getElementById('edit-specific-date');
        
        // Check if deadline is a specific date (YYYY-MM-DD format)
        var deadline = activeGoal.deadline || "Monthly";
        var isSpecificDate = /^\d{4}-\d{2}-\d{2}$/.test(deadline);
        
        if (isSpecificDate) {
            deadlineType.value = 'specific-date';
            specificDateBlock.style.display = 'block';
            specificDateInput.value = deadline;
        } else {
            deadlineType.value = 'end-of-month';
            specificDateBlock.style.display = 'none';
            specificDateInput.value = '';
        }
    };
}

// Toggle date picker visibility when deadline type changes
var deadlineTypeSelect = document.getElementById('edit-deadline-type');
if (deadlineTypeSelect) {
    deadlineTypeSelect.onchange = function() {
        var specificDateBlock = document.getElementById('specific-date-block');
        if (this.value === 'specific-date') {
            specificDateBlock.style.display = 'block';
        } else {
            specificDateBlock.style.display = 'none';
        }
    };
}

if (cancelEditBtn) {
    cancelEditBtn.onclick = () => {
        summaryView.style.display = 'block';
        editView.style.display = 'none';
    };
}

saveTargetBtn.onclick = async function() {
    var btn = this;
    var newAmount = parseFloat(document.getElementById('edit-amount').value) || 0;
    
    // Get deadline value
    var deadlineType = document.getElementById('edit-deadline-type').value;
    var newDeadline = "Monthly"; // Default
    
    if (deadlineType === 'specific-date') {
        var specificDate = document.getElementById('edit-specific-date').value;
        if (specificDate) {
            newDeadline = specificDate; // Store as YYYY-MM-DD
        }
    } else {
        newDeadline = "Monthly";
    }
    
    // Disable button and show saving state
    btn.disabled = true;
    var originalText = btn.textContent;
    btn.textContent = "Saving...";
    
    try {
        var res = await fetch('http://127.0.0.1:8000/update-goal', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_id: getLoamyUserId(),
                item: activeGoal.item,
                amount: newAmount,
                deadline: newDeadline,
                category: activeGoal.category || "goal",
                assigned: activeGoal.assigned || 0
            })
        });
        
        if (res.ok) {
            // Update the activeGoal object
            activeGoal.amount = newAmount;
            activeGoal.deadline = newDeadline;
            
            // Switch back to summary view and refresh panel
            summaryView.style.display = 'block';
            editView.style.display = 'none';
            openPanel(activeGoal);
            
            // Reload the main list
            loadData();
        } else {
            alert("Failed to save. Please try again.");
        }
    } catch (e) {
        console.error("Save failed:", e);
        alert("Connection error. Is the backend running?");
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
};

// Assign Button - assigns user-specified amount from Ready to Assign to the selected goal
var assignBtn = document.getElementById('btn-assign');
if (assignBtn) {
    assignBtn.onclick = async function() {
        if (!activeGoal) return;
        
        var btn = this;
        var targetAmount = activeGoal.amount || 0;
        var assignedAmount = activeGoal.assigned || 0;
        var remainingAmount = Math.max(0, targetAmount - assignedAmount);
        
        // Get the user-entered amount from the input field
        var assignInput = document.getElementById('assign-amount-input');
        var amountToAssign = parseFloat(assignInput.value) || 0;
        
        // Validate the amount
        if (amountToAssign <= 0) {
            alert("Please enter an amount to assign.");
            assignInput.focus();
            return;
        }
        
        if (amountToAssign > remainingAmount) {
            alert("You cannot assign more than the remaining amount ($" + remainingAmount.toLocaleString() + ").");
            return;
        }
        
        if (amountToAssign > totalReadyToAssign) {
            alert("You don't have enough funds. Available: $" + totalReadyToAssign.toLocaleString());
            return;
        }
        
        if (totalReadyToAssign <= 0) {
            alert("No funds available to assign. Add money to an account first.");
            return;
        }
        
        btn.disabled = true;
        var originalText = btn.textContent;
        btn.textContent = "Assigning...";
        
        try {
            var res = await fetch('http://127.0.0.1:8000/assign-to-goal', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    item: activeGoal.item,
                    amount: amountToAssign
                })
            });
            
            if (res.ok) {
                var data = await res.json();
                
                // Update local state
                activeGoal.assigned = data.assigned;
                totalReadyToAssign -= amountToAssign;
                
                // Clear the input field
                assignInput.value = '';
                
                // Update the displays
                updateReadyToAssign();
                openPanel(activeGoal);
                
                // Reload to reflect changes
                loadData();
            } else {
                alert("Failed to assign funds. Please try again.");
            }
        } catch (e) {
            console.error("Assign failed:", e);
            alert("Connection error. Is the backend running?");
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    };
}

// Edit Goal Name Button (pencil icon in panel header)
var editGoalNameBtn = document.getElementById('edit-goal-name-btn');
var editGoalModal = document.getElementById('edit-goal-modal');
var editGoalNameInput = document.getElementById('edit-goal-name-input');
var cancelEditGoalBtn = document.getElementById('cancel-edit-goal');
var saveEditGoalBtn = document.getElementById('save-edit-goal');

if (editGoalNameBtn) {
    editGoalNameBtn.onclick = function() {
        if (!activeGoal) return;
        editGoalNameInput.value = activeGoal.item;
        editGoalModal.style.display = 'flex';
        editGoalNameInput.focus();
    };
}

if (cancelEditGoalBtn) {
    cancelEditGoalBtn.onclick = function() {
        editGoalModal.style.display = 'none';
    };
}

if (saveEditGoalBtn) {
    saveEditGoalBtn.onclick = async function() {
        if (!activeGoal) return;
        
        var newName = editGoalNameInput.value.trim();
        if (!newName) {
            alert("Please enter a name.");
            return;
        }
        
        var btn = this;
        btn.disabled = true;
        var originalText = btn.textContent;
        btn.textContent = "Saving...";
        
        try {
            var res = await fetch('http://127.0.0.1:8000/rename-goal', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    old_name: activeGoal.item,
                    new_name: newName
                })
            });
            
            if (res.ok) {
                var data = await res.json();
                activeGoal.item = data.new_name;
                editGoalModal.style.display = 'none';
                openPanel(activeGoal);
                loadData();
            } else {
                alert("Failed to rename. Please try again.");
            }
        } catch (e) {
            console.error("Rename failed:", e);
            alert("Connection error. Is the backend running?");
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    };
}

// Delete Goal Button (trash icon in panel header)
var deleteGoalBtn = document.getElementById('delete-goal-btn');
var deleteGoalModal = document.getElementById('delete-goal-modal');
var deleteGoalTitle = document.getElementById('delete-goal-title');
var cancelDeleteGoalBtn = document.getElementById('cancel-delete-goal');
var confirmDeleteGoalBtn = document.getElementById('confirm-delete-goal');

if (deleteGoalBtn) {
    deleteGoalBtn.onclick = function() {
        if (!activeGoal) return;
        
        // Set the modal title based on category
        var categoryText = (activeGoal.category === 'bill') ? 'bill' : 'goal';
        deleteGoalTitle.textContent = 'Delete this ' + categoryText + '?';
        deleteGoalModal.style.display = 'flex';
    };
}

if (cancelDeleteGoalBtn) {
    cancelDeleteGoalBtn.onclick = function() {
        deleteGoalModal.style.display = 'none';
    };
}

if (confirmDeleteGoalBtn) {
    confirmDeleteGoalBtn.onclick = async function() {
        if (!activeGoal) return;
        
        var btn = this;
        btn.disabled = true;
        
        try {
            var res = await fetch('http://127.0.0.1:8000/delete-goal', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: getLoamyUserId(), item: activeGoal.item })
            });
            
            if (res.ok) {
                deleteGoalModal.style.display = 'none';
                sidePanel.classList.remove('active');
                activeGoal = null;
                loadData();
            } else {
                alert("Failed to delete. Please try again.");
            }
        } catch (e) {
            console.error("Delete failed:", e);
            alert("Connection error. Is the backend running?");
        } finally {
            btn.disabled = false;
        }
    };
}

triggerAddBtn.onclick = () => modal.style.display = 'flex';

// Toggle date picker in bill modal
var billDeadlineType = document.getElementById('new-bill-deadline-type');
var billDateBlock = document.getElementById('new-bill-date-block');

if (billDeadlineType) {
    billDeadlineType.onchange = function() {
        if (this.value === 'specific') {
            billDateBlock.style.display = 'block';
        } else {
            billDateBlock.style.display = 'none';
        }
    };
}

cancelBillBtn.onclick = () => {
    modal.style.display = 'none';
    inputName.value = "";
    inputBillAmount.value = "";
    if (billDeadlineType) billDeadlineType.value = "Monthly";
    if (billDateBlock) billDateBlock.style.display = 'none';
    var specificDateInput = document.getElementById('new-bill-specific-date');
    if (specificDateInput) specificDateInput.value = "";
};

saveBillBtn.onclick = async () => {
    const name = toSentenceCase(inputName.value.trim());
    const amount = parseFloat(inputBillAmount.value) || 0;
    
    // Get deadline
    var deadlineTypeValue = billDeadlineType ? billDeadlineType.value : "Monthly";
    var deadline = deadlineTypeValue;
    
    if (deadlineTypeValue === 'specific') {
        var specificDate = document.getElementById('new-bill-specific-date').value;
        if (specificDate) {
            deadline = specificDate; // Store as YYYY-MM-DD
        } else {
            deadline = "Monthly";
        }
    }
    
    if (name) {
        try {
            // Use the dedicated add-bill endpoint so it goes to BILLS category
            await fetch('http://127.0.0.1:8000/add-bill', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ item: name, amount: amount, deadline: deadline })
            });
            modal.style.display = 'none';
            inputName.value = "";
            inputBillAmount.value = "";
            if (billDeadlineType) billDeadlineType.value = "Monthly";
            if (billDateBlock) billDateBlock.style.display = 'none';
            var specificDateInput = document.getElementById('new-bill-specific-date');
            if (specificDateInput) specificDateInput.value = "";
            loadData();
        } catch (e) { console.error(e); }
    }
};

// Account Modal Controls
var openAccountBtn = document.getElementById('open-account-modal');
var closeAccountBtn = document.getElementById('close-account-modal');
var backAccountBtn = document.getElementById('back-account-modal');

if (openAccountBtn) {
    openAccountBtn.onclick = function() {
        accountToEdit = null;
        document.getElementById('acc-nickname').value = '';
        document.getElementById('acc-balance').value = '';
        document.getElementById('acc-type').value = 'Checking';
        accountModal.style.display = 'flex';
        lucide.createIcons();
    };
}

// Close account modal (X button)
if (closeAccountBtn) {
    closeAccountBtn.onclick = function() {
        accountModal.style.display = 'none';
        accountToEdit = null;
    };
}

// Back button in account modal (chevron-left)
if (backAccountBtn) {
    backAccountBtn.onclick = function() {
        accountModal.style.display = 'none';
        accountToEdit = null;
    };
}

// Save Account (handles both Add and Edit)
document.getElementById('save-account').onclick = async function() {
    const btn = this;
    const nickname = document.getElementById('acc-nickname').value.trim();
    const balance = parseFloat(document.getElementById('acc-balance').value);
    const type = document.getElementById('acc-type').value;

    if (!nickname || isNaN(balance)) {
        alert("Please enter a valid name and balance.");
        return;
    }

    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Saving...";

    try {
        let res;
        if (accountToEdit) {
            // Edit existing account
            res = await fetch(`http://127.0.0.1:8000/update-account/${accountToEdit.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: getLoamyUserId(), nickname, balance, type })
            });
        } else {
            // Add new account
            res = await fetch('http://127.0.0.1:8000/add-account', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: getLoamyUserId(), nickname, balance, type })
            });
        }

        if (res.ok) {
            accountModal.style.display = 'none';
            accountToEdit = null;
            document.getElementById('acc-nickname').value = '';
            document.getElementById('acc-balance').value = '';
            loadData();
        } else {
            alert("Server error. Please try again.");
        }
    } catch (error) {
        console.error("Connection failed:", error);
        alert("Backend Offline. Check your terminal!");
    } finally {
        btn.disabled = false;
        btn.textContent = originalText;
    }
};

// Delete Account Confirmation
document.getElementById('confirm-delete').onclick = async function() {
    if (!accountToDelete) return;
    this.disabled = true;
    try {
        await fetch(`http://127.0.0.1:8000/delete-account/${accountToDelete}`, { method: 'DELETE' });
        deleteModal.style.display = 'none';
        accountToDelete = null;
        loadData();
    } catch (error) {
        console.error("Delete failed:", error);
        alert("Failed to delete account.");
    } finally {
        this.disabled = false;
    }
};

document.getElementById('cancel-delete').onclick = () => {
    deleteModal.style.display = 'none';
    accountToDelete = null;
};

// --- Section Toggle (Collapse/Expand) ---
document.querySelectorAll('.group-header[data-toggle]').forEach(function(header) {
    header.addEventListener('click', function(e) {
        // Don't toggle if clicking the add button
        if (e.target.closest('.add-row-btn')) return;
        
        var section = this.closest('.category-group');
        section.classList.toggle('collapsed');
    });
});

// Close modals on outside click
window.onclick = (e) => {
    if (e.target === accountModal) {
        accountModal.style.display = 'none';
        accountToEdit = null;
    }
    if (e.target === deleteModal) {
        deleteModal.style.display = 'none';
        accountToDelete = null;
    }
    if (e.target === modal) {
        modal.style.display = 'none';
    }
    if (e.target === editGoalModal) {
        editGoalModal.style.display = 'none';
    }
    if (e.target === deleteGoalModal) {
        deleteGoalModal.style.display = 'none';
    }
};

document.addEventListener('DOMContentLoaded', loadData);
