/**
 * Fintech AI - Spending Analysis Logic
 * Handles expense tracking, display, and summaries
 */

// Resolve the logged-in user so every request is scoped to their account.
function getLoamyUserId() {
    try {
        var s = JSON.parse(localStorage.getItem('fintech_session') || '{}');
        return s.user_id || 'default';
    } catch (e) {
        return 'default';
    }
}

// Category icons (emoji for simplicity)
var categoryIcons = {
    food: "🍔",
    transport: "🚗",
    shopping: "🛍️",
    bills: "📄",
    entertainment: "🎬",
    other: "💰"
};

var categoryLabels = {
    food: "Food & Dining",
    transport: "Transport",
    shopping: "Shopping",
    bills: "Bills & Utilities",
    entertainment: "Entertainment",
    other: "Other"
};

// State
var selectedTransaction = null;
var editingTransaction = null;
var currentFilter = "all";

// DOM Elements
var transactionsList = document.getElementById('transactions-list');
var emptyState = document.getElementById('empty-state');
var detailPanel = document.getElementById('detail-panel');
var expenseModal = document.getElementById('expense-modal');
var deleteModal = document.getElementById('delete-modal');

// Summary elements
var spentToday = document.getElementById('spent-today');
var spentWeek = document.getElementById('spent-week');
var spentMonth = document.getElementById('spent-month');

/**
 * Format currency
 */
function formatCurrency(amount) {
    return '$' + parseFloat(amount).toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    });
}

/**
 * Format date for display
 */
function formatDate(dateStr) {
    var date = new Date(dateStr);
    var today = new Date();
    var yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);

    if (date.toDateString() === today.toDateString()) {
        return "Today";
    } else if (date.toDateString() === yesterday.toDateString()) {
        return "Yesterday";
    } else {
        return date.toLocaleDateString('en-US', { 
            weekday: 'long', 
            month: 'short', 
            day: 'numeric' 
        });
    }
}

/**
 * Format time for display
 */
function formatTime(dateStr) {
    var date = new Date(dateStr);
    return date.toLocaleTimeString('en-US', { 
        hour: 'numeric', 
        minute: '2-digit',
        hour12: true 
    });
}

/**
 * Group transactions by date
 */
function groupByDate(transactions) {
    var groups = {};
    transactions.forEach(function(t) {
        var dateKey = new Date(t.date).toDateString();
        if (!groups[dateKey]) {
            groups[dateKey] = [];
        }
        groups[dateKey].push(t);
    });
    return groups;
}

/**
 * Create transaction item HTML
 */
function createTransactionItem(transaction) {
    var item = document.createElement('div');
    item.className = 'transaction-item';
    item.setAttribute('data-id', transaction.id);

    // Get category label with fallback
    var catKey = transaction.category || 'other';
    var categoryLabel = categoryLabels[catKey] || transaction.category || 'Other';

    item.innerHTML = 
        '<div class="transaction-info">' +
            '<div class="transaction-description">' + (transaction.description || 'Unknown') + '</div>' +
            '<div class="transaction-category">' + categoryLabel + '</div>' +
        '</div>' +
        '<div class="transaction-amount">-' + formatCurrency(transaction.amount || 0) + '</div>' +
        '<div class="transaction-time">' + formatTime(transaction.date) + '</div>';

    // Click handler to show details
    item.onclick = function() {
        selectTransaction(transaction, item);
    };

    return item;
}

/**
 * Select a transaction and show details
 */
function selectTransaction(transaction, element) {
    // Remove previous selection
    var prevSelected = document.querySelector('.transaction-item.selected');
    if (prevSelected) {
        prevSelected.classList.remove('selected');
    }

    // Add selection to current
    element.classList.add('selected');
    selectedTransaction = transaction;

    // Update detail panel
    var catKey = transaction.category || 'other';
    var categoryLabel = categoryLabels[catKey] || transaction.category || 'Other';
    
    var panelContent = document.getElementById('panel-content');
    panelContent.innerHTML = 
        '<div class="detail-item">' +
            '<div class="detail-label">Amount</div>' +
            '<div class="detail-value amount">-' + formatCurrency(transaction.amount || 0) + '</div>' +
        '</div>' +
        '<div class="detail-item">' +
            '<div class="detail-label">Description</div>' +
            '<div class="detail-value">' + (transaction.description || 'Unknown') + '</div>' +
        '</div>' +
        '<div class="detail-item">' +
            '<div class="detail-label">Category</div>' +
            '<div class="detail-value">' + categoryLabel + '</div>' +
        '</div>' +
        '<div class="detail-item">' +
            '<div class="detail-label">Date</div>' +
            '<div class="detail-value">' + formatDate(transaction.date) + ' at ' + formatTime(transaction.date) + '</div>' +
        '</div>' +
        (transaction.notes ? 
            '<div class="detail-item">' +
                '<div class="detail-label">Notes</div>' +
                '<div class="detail-value">' + transaction.notes + '</div>' +
            '</div>' : '');

    // Show panel
    detailPanel.classList.add('active');
}

/**
 * Load and display transactions
 */
async function loadTransactions() {
    try {
        var response = await fetch('http://127.0.0.1:8000/get-expenses?user_id=' + encodeURIComponent(getLoamyUserId()));
        var data = await response.json();

        if (!data.expenses || data.expenses.length === 0) {
            emptyState.style.display = 'flex';
            updateSummaries([]);
            return;
        }

        emptyState.style.display = 'none';

        // Filter by category if needed
        var filtered = data.expenses;
        if (currentFilter !== "all") {
            filtered = data.expenses.filter(function(e) {
                return e.category === currentFilter;
            });
        }

        // Sort by date (newest first)
        filtered.sort(function(a, b) {
            return new Date(b.date) - new Date(a.date);
        });

        // Group by date
        var groups = groupByDate(filtered);

        // Clear list (except empty state)
        transactionsList.innerHTML = '';

        // Render groups
        Object.keys(groups).sort(function(a, b) {
            return new Date(b) - new Date(a);
        }).forEach(function(dateKey) {
            var group = document.createElement('div');
            group.className = 'transaction-date-group';

            var header = document.createElement('div');
            header.className = 'date-header';
            header.textContent = formatDate(dateKey);
            group.appendChild(header);

            groups[dateKey].forEach(function(transaction) {
                group.appendChild(createTransactionItem(transaction));
            });

            transactionsList.appendChild(group);
        });

        // Update summaries with all data (not filtered)
        updateSummaries(data.expenses);

        lucide.createIcons();

    } catch (error) {
        console.error('Failed to load transactions:', error);
        emptyState.style.display = 'flex';
    }
}

/**
 * Update summary cards
 */
function updateSummaries(expenses) {
    var today = new Date();
    today.setHours(0, 0, 0, 0);

    var startOfWeek = new Date(today);
    startOfWeek.setDate(today.getDate() - today.getDay());

    var startOfMonth = new Date(today.getFullYear(), today.getMonth(), 1);

    var todayTotal = 0;
    var weekTotal = 0;
    var monthTotal = 0;

    expenses.forEach(function(e) {
        var expenseDate = new Date(e.date);
        expenseDate.setHours(0, 0, 0, 0);

        if (expenseDate >= today) {
            todayTotal += e.amount;
        }
        if (expenseDate >= startOfWeek) {
            weekTotal += e.amount;
        }
        if (expenseDate >= startOfMonth) {
            monthTotal += e.amount;
        }
    });

    spentToday.textContent = formatCurrency(todayTotal);
    spentWeek.textContent = formatCurrency(weekTotal);
    spentMonth.textContent = formatCurrency(monthTotal);
}

/**
 * Save expense (add or edit)
 */
async function saveExpense() {
    var description = document.getElementById('expense-description').value.trim();
    var amount = parseFloat(document.getElementById('expense-amount').value);
    var category = document.getElementById('expense-category').value;
    var date = document.getElementById('expense-date').value;
    var notes = document.getElementById('expense-notes').value.trim();

    if (!description) {
        alert('Please enter a description');
        return;
    }

    if (!amount || amount <= 0) {
        alert('Please enter a valid amount');
        return;
    }

    if (!date) {
        alert('Please select a date');
        return;
    }

    var btn = document.getElementById('save-expense');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        var endpoint = editingTransaction ? 
            'http://127.0.0.1:8000/update-expense' : 
            'http://127.0.0.1:8000/add-expense';

        var method = editingTransaction ? 'PUT' : 'POST';

        var body = {
            user_id: getLoamyUserId(),
            description: description,
            amount: amount,
            category: category,
            date: date + 'T' + new Date().toTimeString().slice(0, 8),
            notes: notes
        };

        if (editingTransaction) {
            body.id = editingTransaction.id;
        }

        var response = await fetch(endpoint, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (response.ok) {
            closeExpenseModal();
            loadTransactions();
        } else {
            alert('Failed to save expense. Please try again.');
        }

    } catch (error) {
        console.error('Save error:', error);
        alert('Connection error. Is the backend running?');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Save Expense';
    }
}

/**
 * Delete expense
 */
async function deleteExpense() {
    if (!selectedTransaction) return;

    var btn = document.getElementById('confirm-delete');
    btn.disabled = true;

    try {
        var response = await fetch('http://127.0.0.1:8000/delete-expense', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: selectedTransaction.id })
        });

        if (response.ok) {
            deleteModal.style.display = 'none';
            detailPanel.classList.remove('active');
            selectedTransaction = null;
            loadTransactions();
        } else {
            alert('Failed to delete. Please try again.');
        }

    } catch (error) {
        console.error('Delete error:', error);
        alert('Connection error. Is the backend running?');
    } finally {
        btn.disabled = false;
    }
}

/**
 * Open expense modal for adding
 */
function openExpenseModal() {
    editingTransaction = null;
    document.getElementById('modal-title').textContent = 'Add Expense';
    document.getElementById('expense-description').value = '';
    document.getElementById('expense-amount').value = '';
    document.getElementById('expense-category').value = 'food';
    document.getElementById('expense-date').value = new Date().toISOString().split('T')[0];
    document.getElementById('expense-notes').value = '';
    expenseModal.style.display = 'flex';
    lucide.createIcons();
}

/**
 * Open expense modal for editing
 */
function openEditModal() {
    if (!selectedTransaction) return;
    editingTransaction = selectedTransaction;
    document.getElementById('modal-title').textContent = 'Edit Expense';
    document.getElementById('expense-description').value = selectedTransaction.description;
    document.getElementById('expense-amount').value = selectedTransaction.amount;
    document.getElementById('expense-category').value = selectedTransaction.category;
    document.getElementById('expense-date').value = selectedTransaction.date.split('T')[0];
    document.getElementById('expense-notes').value = selectedTransaction.notes || '';
    expenseModal.style.display = 'flex';
    lucide.createIcons();
}

/**
 * Close expense modal
 */
function closeExpenseModal() {
    expenseModal.style.display = 'none';
    editingTransaction = null;
}

// Event Listeners
document.getElementById('open-expense-modal').onclick = openExpenseModal;
document.getElementById('close-expense-modal').onclick = closeExpenseModal;
document.getElementById('save-expense').onclick = saveExpense;

document.getElementById('close-detail-panel').onclick = function() {
    detailPanel.classList.remove('active');
    var prevSelected = document.querySelector('.transaction-item.selected');
    if (prevSelected) prevSelected.classList.remove('selected');
    selectedTransaction = null;
};

document.getElementById('edit-transaction-btn').onclick = openEditModal;

document.getElementById('delete-transaction-btn').onclick = function() {
    if (selectedTransaction) {
        deleteModal.style.display = 'flex';
    }
};

document.getElementById('cancel-delete').onclick = function() {
    deleteModal.style.display = 'none';
};

document.getElementById('confirm-delete').onclick = deleteExpense;

// Category filter
document.getElementById('category-filter').onchange = function(e) {
    currentFilter = e.target.value;
    loadTransactions();
};

// Close modals on outside click
window.onclick = function(e) {
    if (e.target === expenseModal) {
        closeExpenseModal();
    }
    if (e.target === deleteModal) {
        deleteModal.style.display = 'none';
    }
};

// Initialize
document.addEventListener('DOMContentLoaded', function() {
    loadTransactions();
    lucide.createIcons();
});
