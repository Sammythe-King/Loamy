// --- Fintech AI Frontend Logic (Updated for JSON & Chat) ---

// ===== SESSION CHECK - Redirect to login if not authenticated =====
var sessionData = localStorage.getItem('fintech_session');
if (!sessionData) {
    window.location.href = 'login.html';
}

// Parse session for user info
var currentUser = sessionData ? JSON.parse(sessionData) : null;

// Helper function to get current user ID
function getCurrentUserId() {
    return currentUser ? currentUser.user_id : null;
}

// Helper function to get current user email
function getCurrentUserEmail() {
    return currentUser ? currentUser.email : null;
}

const chatDisplay = document.getElementById('chat-display');
const userInput = document.getElementById('user-input');
const sendBtn = document.querySelector('.send-btn');
const fileInput = document.getElementById('receipt-upload');
const welcomeScreen = document.querySelector('.welcome-container');
const chatListContainer = document.getElementById('chat-list');
const newChatBtn = document.querySelector('.new-chat-btn');
const gmailBtn = document.getElementById('gmail-btn');

// Gmail button click handler - opens Gmail connect page
if (gmailBtn) {
    gmailBtn.addEventListener('click', function() {
        window.location.href = 'gmail-connect.html';
    });
    
    // Check if Gmail is connected and update button style
    if (localStorage.getItem('gmail_connected') === 'true') {
        gmailBtn.classList.add('connected');
        gmailBtn.title = 'Gmail Connected';
    }
}

// ===== Display current user info =====
if (currentUser) {
    var userAvatar = document.getElementById('user-avatar');
    var userName = document.getElementById('user-name');
    
    if (userAvatar && currentUser.name) {
        userAvatar.textContent = currentUser.name.charAt(0).toUpperCase();
    }
    if (userName && currentUser.name) {
        userName.textContent = currentUser.name;
    }
}

// ===== Logout button handler =====
var logoutBtn = document.getElementById('logout-btn');
if (logoutBtn) {
    logoutBtn.addEventListener('click', function() {
        if (confirm('Are you sure you want to logout?')) {
            localStorage.removeItem('fintech_session');
            localStorage.removeItem('gmail_connected');
            localStorage.removeItem('gmail_data');
            window.location.href = 'login.html';
        }
    });
}

// Current chat state
var currentChatId = null;
var currentMessages = [];

/**
 * Generate a smart, personalized title from user's message (max 5 words)
 */
function generateSmartTitle(message) {
    var text = message.toLowerCase().trim();
    
    // Remove filler words
    var fillers = ['help', 'please', 'can you', 'could you', 'i want to', 'i need to', 
                   'i would like to', 'how do i', 'what is', 'tell me', 'show me',
                   'the', 'a', 'an', 'my', 'me', 'i'];
    
    var clean = text;
    fillers.forEach(function(f) {
        clean = clean.replace(new RegExp('\\b' + f + '\\b', 'gi'), '');
    });
    clean = clean.replace(/\s+/g, ' ').trim();
    
    // Detect savings/goals
    if (text.indexOf('save') !== -1 || text.indexOf('saving') !== -1 || text.indexOf('goal') !== -1) {
        var match = text.match(/(?:save|saving|goal)\s*(?:for|towards?)?\s*(?:\$[\d,]+\s*(?:for)?)?\s*(.+?)(?:\s+by|\s+until|\s*$)/);
        if (match && match[1]) {
            var item = match[1].replace(/^\s*(a|an|the|my)\s+/, '').trim();
            var words = item.split(' ').slice(0, 3);
            return words.map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(' ') + ' Savings';
        }
    }
    
    // Detect budget questions
    if (text.indexOf('budget') !== -1) {
        return 'Budget Review';
    }
    
    // Detect spending analysis
    if (text.indexOf('spend') !== -1 || text.indexOf('spending') !== -1) {
        return 'Spending Analysis';
    }
    
    // Detect affordability questions
    if (text.indexOf('afford') !== -1) {
        var match = text.match(/afford\s+(?:a|an|the)?\s*(\w+)/);
        if (match && match[1]) {
            return match[1].charAt(0).toUpperCase() + match[1].slice(1) + ' Affordability';
        }
    }
    
    // Detect month-specific queries
    var months = ['january', 'february', 'march', 'april', 'may', 'june', 
                  'july', 'august', 'september', 'october', 'november', 'december'];
    for (var i = 0; i < months.length; i++) {
        if (text.indexOf(months[i]) !== -1) {
            var monthName = months[i].charAt(0).toUpperCase() + months[i].slice(1);
            return monthName + ' Financial Plan';
        }
    }
    
    // Default: extract key words (max 4 words)
    clean = clean.replace(/[^\w\s]/g, '');
    var stopWords = ['and', 'the', 'for', 'you', 'how', 'what', 'this', 'that', 'with', 'have', 'any'];
    var words = clean.split(' ').filter(function(w) { 
        return w.length > 2 && stopWords.indexOf(w) === -1; 
    });
    
    if (words.length > 0) {
        var titleWords = words.slice(0, 4);
        return titleWords.map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(' ');
    }
    
    return 'Financial Consultation';
}

/**
 * Generate a personalized title for receipt uploads
 * Rules:
 * - Primary: Vendor name + category suffix (e.g., "KFC Dining Expense")
 * - Fallback: If vendor unavailable, use category only (e.g., "Food Purchase")
 */
function generateReceiptTitle(vendor, category) {
    var vendorClean = (vendor || '').trim();
    var catLower = (category || 'other').toLowerCase();
    var hasVendor = vendorClean && vendorClean.toLowerCase() !== 'unknown' && vendorClean.length > 0;
    
    // Common vendor mappings (known brands get special titles)
    var vendorTitles = {
        'kfc': 'KFC Dining Expense',
        'starbucks': 'Starbucks Coffee Run',
        'mcdonalds': 'McDonald\'s Purchase',
        'walmart': 'Walmart Shopping Trip',
        'amazon': 'Amazon Order',
        'uber': 'Uber Ride Expense',
        'lyft': 'Lyft Ride Expense',
        'target': 'Target Shopping Trip',
        'costco': 'Costco Bulk Purchase',
        'whole foods': 'Whole Foods Groceries',
        'trader joe': 'Trader Joe\'s Groceries',
    };
    
    // Check for known vendors first
    if (hasVendor) {
        var vendorLower = vendorClean.toLowerCase();
        for (var key in vendorTitles) {
            if (vendorLower.indexOf(key) !== -1) {
                return vendorTitles[key];
            }
        }
    }
    
    // Category suffix mappings
    var categorySuffixes = {
        'food': 'Dining Expense',
        'dining': 'Dining Expense',
        'restaurant': 'Restaurant Visit',
        'grocery': 'Grocery Shopping',
        'groceries': 'Grocery Shopping',
        'shopping': 'Shopping Trip',
        'retail': 'Shopping Trip',
        'transport': 'Transport Expense',
        'transportation': 'Transport Expense',
        'travel': 'Travel Expense',
        'entertainment': 'Entertainment',
        'utilities': 'Utility Bill',
        'subscription': 'Subscription',
        'healthcare': 'Healthcare Expense',
        'medical': 'Medical Expense',
    };
    
    // Find matching category suffix
    var suffix = 'Purchase';
    for (var cat in categorySuffixes) {
        if (catLower.indexOf(cat) !== -1) {
            suffix = categorySuffixes[cat];
            break;
        }
    }
    
    // If vendor available: "VendorName Suffix"
    // If no vendor: just "Category Suffix"
    if (hasVendor) {
        // Capitalize vendor name properly
        var vendorTitle = vendorClean.split(' ').map(function(w) {
            return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
        }).join(' ');
        return vendorTitle + ' ' + suffix;
    } else {
        // No vendor - use category-based title
        var catTitle = catLower.charAt(0).toUpperCase() + catLower.slice(1);
        return catTitle + ' ' + suffix;
    }
}

/**
 * Simple markdown parser to convert markdown text to HTML
 * Handles: headings, bold, italic, bullet lists, numbered lists, line breaks
 */
function parseMarkdown(text) {
    if (!text) return '';
    
    // Escape HTML to prevent XSS
    var html = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    
    // Convert markdown to HTML
    
    // Headers (## Header)
    html = html.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
    
    // Bold (**text** or __text__)
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
    
    // Italic (*text* or _text_)
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(/_([^_]+)_/g, '<em>$1</em>');
    
    // Bullet lists (- item or * item)
    html = html.replace(/^\s*[-*]\s+(.+)$/gm, '<li>$1</li>');
    
    // Numbered lists (1. item)
    html = html.replace(/^\s*\d+\.\s+(.+)$/gm, '<li>$1</li>');
    
    // Wrap consecutive <li> elements in <ul>
    html = html.replace(/(<li>.*<\/li>)(\s*<li>)/g, '$1$2');
    html = html.replace(/(<li>.*<\/li>)/gs, function(match) {
        return '<ul>' + match + '</ul>';
    });
    
    // Clean up multiple <ul> tags that got nested
    html = html.replace(/<\/ul>\s*<ul>/g, '');
    
    // Line breaks (double newline = paragraph break)
    html = html.replace(/\n\n/g, '</p><p>');
    html = html.replace(/\n/g, '<br>');
    
    // Wrap in paragraph tags
    html = '<p>' + html + '</p>';
    
    // Clean up empty paragraphs
    html = html.replace(/<p>\s*<\/p>/g, '');
    html = html.replace(/<p>\s*<br>\s*<\/p>/g, '');
    
    // Fix paragraph tags around block elements
    html = html.replace(/<p>\s*(<h[234]>)/g, '$1');
    html = html.replace(/(<\/h[234]>)\s*<\/p>/g, '$1');
    html = html.replace(/<p>\s*(<ul>)/g, '$1');
    html = html.replace(/(<\/ul>)\s*<\/p>/g, '$1');
    
    return html;
}

/**
 * Creates a clean HTML table from the JSON data extracted by the AI.
 * Added safety checks to prevent crashes if data is missing.
 */
function createSummaryTable(data) {
    // If the AI didn't return an object, return the raw content instead
    if (typeof data !== 'object' || data === null) {
        return `<div class="ai-text-response">${data}</div>`;
    }

    const total = typeof data.total === 'number' ? data.total.toFixed(2) : (data.total || '0.00');

    return `
        <div class="summary-container">
            <div class="verification-tag">
                <i data-lucide="check-circle" style="width:16px; color:#b3ea57;"></i>
                <strong>Verification Confirmed:</strong> ${data.dpc_logic}
            </div>
            <div class="receipt-details">
                <p><strong>Vendor:</strong> ${data.vendor}</p>
                <p><strong>Date:</strong> ${data.date}</p>
                <p><strong>Category:</strong> ${data.category}</p>
                <p class="total-highlight"><strong>Total:</strong> $${data.total.toFixed(2)}</p>
            </div>
        </div>
    `;
}

/**
 * Adds a message bubble to the chat display.
 * AI messages are parsed as markdown for better formatting.
 */
function addMessage(content, sender, isHTML = false) {
    if (welcomeScreen) welcomeScreen.style.display = 'none';

    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${sender}-message`;
    
    if (isHTML) {
        // Already HTML (like tables)
        messageDiv.innerHTML = content;
        setTimeout(() => lucide.createIcons(), 10);
    } else if (sender === 'ai') {
        // Parse markdown for AI responses
        messageDiv.innerHTML = parseMarkdown(content);
    } else {
        // User messages stay as plain text
        messageDiv.textContent = content;
    }
    
    chatDisplay.appendChild(messageDiv);
    chatDisplay.scrollTop = chatDisplay.scrollHeight;
}

// --- File Preview & Upload Logic ---
var pendingFile = null;
var filePreviewContainer = document.getElementById('file-preview-container');
var filePreviewImage = document.getElementById('file-preview-image');
var filePreviewName = document.getElementById('file-preview-name');
var fileRemoveBtn = document.getElementById('file-remove-btn');

// When user selects a file, show preview (don't upload yet)
fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (!file) return;

    pendingFile = file;
    
    // Show preview
    if (file.type.startsWith('image/')) {
        var reader = new FileReader();
        reader.onload = function(e) {
            filePreviewImage.src = e.target.result;
        };
        reader.readAsDataURL(file);
    } else {
        // PDF or other file - show generic icon
        filePreviewImage.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%23666"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8" fill="%23999"/></svg>';
    }
    
    filePreviewName.textContent = file.name.length > 15 ? file.name.substring(0, 12) + '...' : file.name;
    filePreviewContainer.style.display = 'block';
    
    // Re-init icons for the X button
    lucide.createIcons();
    
    fileInput.value = '';
});

// Remove file button
fileRemoveBtn.addEventListener('click', () => {
    pendingFile = null;
    filePreviewContainer.style.display = 'none';
    filePreviewImage.src = '';
});

// Upload the pending file (called when user sends)
async function uploadPendingFile() {
    if (!pendingFile) return;
    
    var file = pendingFile;
    pendingFile = null;
    filePreviewContainer.style.display = 'none';
    
    // Hide welcome screen when uploading
    var welcomeContainer = document.querySelector('.welcome-container');
    if (welcomeContainer) {
        welcomeContainer.style.display = 'none';
    }
    
    // Show scanning message (cleaner, no filename)
    addMessage('Scanning receipt...', 'user');
    
    // Add loading message with special ID so we can remove it later
    var loadingDiv = document.createElement('div');
    loadingDiv.className = 'message ai-message';
    loadingDiv.id = 'loading-message';
    loadingDiv.textContent = 'Analyzing artifact and verifying totals...';
    chatDisplay.appendChild(loadingDiv);
    chatDisplay.scrollTop = chatDisplay.scrollHeight;

    const formData = new FormData();
    formData.append("file", file);

    try {
        const response = await fetch('http://127.0.0.1:8000/upload-artifact', {
            method: 'POST',
            body: formData
        });

        // Remove loading message
        var loadingMsg = document.getElementById('loading-message');
        if (loadingMsg) {
            loadingMsg.remove();
        }

        const data = await response.json();
        var aiResponse = '';
        var isHTML = false;
        var chatTitle = 'Receipt Analysis';

        if (data.error) {
            aiResponse = "Error: " + data.error;
        } else {
            if (typeof data.analysis === 'string') {
                aiResponse = data.analysis;
            } else {
                aiResponse = createSummaryTable(data.analysis);
                isHTML = true;
                // Generate title from vendor name and category
                var vendor = data.analysis.vendor || '';
                var category = data.analysis.category || 'Other';
                chatTitle = generateReceiptTitle(vendor, category);
            }
        }
        
        addMessage(aiResponse, 'ai', isHTML);
        
        // Create chat with proper title (vendor + category, NOT filename)
        if (!currentChatId) {
            await createNewChat(chatTitle);
        } else {
            // If chat already exists but has bad title, rename it
            renameChat(currentChatId, chatTitle);
        }
        
        // Save to chat history (use descriptive message, not filename)
        var userMsg = 'Uploaded receipt for analysis';
        if (data.analysis && data.analysis.vendor) {
            userMsg = 'Receipt from ' + data.analysis.vendor;
        }
        
        saveMessagesToChat([
            { sender: 'user', content: userMsg, isHTML: false },
            { sender: 'ai', content: aiResponse, isHTML: isHTML }
        ]);

    } catch (error) {
        addMessage("Network error. Is the backend running?", 'ai');
    }
}

// --- Chat Logic (RAG Pipeline) ---
async function handleTextChat(event) {
    // Prevent the page from refreshing if this is called from a form event
    if (event) event.preventDefault();

    const text = userInput.value.trim();
    
    // If there's a pending file, upload it
    if (pendingFile) {
        await uploadPendingFile();
        return;
    }
    
    if (!text) return;

    // If no current chat, create one with a smart title
    if (!currentChatId) {
        var chatTitle = generateSmartTitle(text);
        await createNewChat(chatTitle);
    }

    addMessage(text, 'user');
    userInput.value = ''; 

    try {
        // Check if user is confirming an allocation (flexible matching)
        var lowerText = text.toLowerCase().trim();
        var confirmWords = ['yes', 'yess', 'yep', 'yeah', 'sure', 'ok', 'okay', 'do it', 'confirm', 'go ahead', 'proceed', 'update'];
        var isConfirmation = confirmWords.some(function(word) {
            return lowerText === word || lowerText.startsWith(word + ' ') || lowerText.includes('yes');
        });
        
        // If confirming and we have pending allocations, execute them
        if (isConfirmation && window.pendingAllocations && window.pendingAllocations.length > 0) {
            try {
                var allocResponse = await fetch('http://127.0.0.1:8000/allocate-funds', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: getCurrentUserId() || 'default', allocations: window.pendingAllocations })
                });
                var allocResult = await allocResponse.json();
                
                if (allocResult.status === 'success' && allocResult.updated.length > 0) {
                    var updatedItems = allocResult.updated.map(function(u) { 
                        return u.item + " ($" + u.new_assigned.toFixed(2) + ")"; 
                    }).join(', ');
                    var msg = "Done! I've updated your dashboard. Updated: " + updatedItems + ".";
                    addMessage(msg, 'ai');
                    
                    saveMessagesToChat([
                        { sender: 'user', content: text, isHTML: false },
                        { sender: 'ai', content: msg, isHTML: false }
                    ]);
                } else {
                    addMessage("No items were updated. The items may not exist or there was an issue. Please try again.", 'ai');
                }
                
                window.pendingAllocations = [];
                return;
            } catch (e) {
                console.error("Allocation Error:", e);
                addMessage("Error updating dashboard: " + e.message, 'ai');
            }
        }
        
        const response = await fetch('http://127.0.0.1:8000/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: text })
        });

        const data = await response.json();
        var reply = data.reply || '';
        
        // Check if AI is proposing allocations - extract them for later confirmation
        if (reply.includes("Shall I update your dashboard") || reply.includes("update your dashboard with these changes")) {
            // Parse allocations from the response
            var parsed = parseAllocationsFromReply(reply);
            if (parsed.length > 0) {
                window.pendingAllocations = parsed;
            }
        }
        
        addMessage(reply, 'ai');
        
        // Save both messages to chat history
        saveMessagesToChat([
            { sender: 'user', content: text, isHTML: false },
            { sender: 'ai', content: reply, isHTML: false }
        ]);
    } catch (error) {
        console.error("Chat Error:", error);
        addMessage("Advisor is currently offline.", 'ai');
    }
}

// Parse allocation amounts from AI response
function parseAllocationsFromReply(reply) {
    var allocations = [];
    
    var lines = reply.split('\n');
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line) continue;
        
        var item = '';
        var amount = 0;
        
        // Pattern 1: "Assign $15.00 to Netflix" or "assign $600.00 to new sneakers"
        var match1 = line.match(/assign\s*\$?([\d,]+\.?\d*)\s*to\s+([A-Za-z][A-Za-z\s]*)/i);
        
        // Pattern 2: "Netflix: Add $15" or "Battle Gear: Add $50"
        var match2 = line.match(/^\*?\*?([A-Za-z][A-Za-z\s]*)\*?\*?:\s*Add\s*\$?([\d,]+\.?\d*)/i);
        
        // Pattern 3: "Netflix: $15.00" or "Ball Park: $300.00"
        var match3 = line.match(/^\*?\*?([A-Za-z][A-Za-z\s]*)\*?\*?:\s*\$?([\d,]+\.?\d*)/);
        
        // Pattern 4: "I'll assign $15.00" with item name somewhere
        var match4 = line.match(/I'll assign\s*(?:the\s*)?(?:full\s*)?\$?([\d,]+\.?\d*)/i);
        
        if (match1) {
            amount = parseFloat(match1[1].replace(',', ''));
            item = match1[2].trim().replace(/\*/g, '');
        } else if (match2) {
            item = match2[1].trim().replace(/\*/g, '');
            amount = parseFloat(match2[2].replace(',', ''));
        } else if (match3) {
            item = match3[1].trim().replace(/\*/g, '');
            amount = parseFloat(match3[2].replace(',', ''));
        }
        
        // Clean up item name
        item = item.replace(/[*:]/g, '').trim();
        
        // Skip summary/keyword lines
        var skipWords = ['total', 'ready to assign', 'balance', 'target', 'assigned', 'goal', 'needed', 'remaining', 'after', 'distribution', 'here', 'this', 'would', 'currently', 'your'];
        var isSkip = skipWords.some(function(w) { return item.toLowerCase().includes(w); });
        
        if (!isSkip && amount > 0 && item.length > 2 && item.length < 30) {
            // Check if this item already exists
            var exists = allocations.some(function(a) { return a.item.toLowerCase() === item.toLowerCase(); });
            if (!exists) {
                allocations.push({ item: item, amount: amount });
            }
        }
    }
    
    return allocations;
}

// Event Listeners
if (sendBtn) {
    sendBtn.addEventListener('click', handleTextChat);
}

if (userInput) {
    userInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            handleTextChat(e);
        }
    });
}

// ========== CHAT HISTORY FUNCTIONS ==========

/**
 * Load all chat conversations into sidebar
 */
var hasFixedTitles = false;
async function loadChatList() {
    if (!chatListContainer) return;
    
    try {
        // Fix bad chat titles once on first load
        if (!hasFixedTitles) {
            hasFixedTitles = true;
            try {
                await fetch('http://127.0.0.1:8000/fix-chat-titles', { method: 'POST' });
            } catch (e) { /* ignore */ }
        }
        
        var res = await fetch('http://127.0.0.1:8000/get-chats');
        var data = await res.json();
        
        chatListContainer.innerHTML = '';
        
        if (!data.chats || data.chats.length === 0) {
            chatListContainer.innerHTML = '<div class="chat-list-empty">No chats yet</div>';
            return;
        }
        
        data.chats.forEach(function(chat) {
            var item = document.createElement('div');
            item.className = 'chat-list-item' + (chat.id === currentChatId ? ' active' : '');
            item.setAttribute('data-id', chat.id);
            
            item.innerHTML = 
                '<span class="chat-title">' + chat.title + '</span>' +
                '<button class="chat-menu-btn" title="Options">' +
                    '<i data-lucide="more-horizontal" style="width:16px; height:16px;"></i>' +
                '</button>';
            
            // Click to load chat
            item.onclick = function(e) {
                if (e.target.closest('.chat-menu-btn')) return;
                loadChat(chat.id);
            };
            
            // Menu button click
            var menuBtn = item.querySelector('.chat-menu-btn');
            menuBtn.onclick = function(e) {
                e.stopPropagation();
                showChatMenu(chat, menuBtn);
            };
            
            chatListContainer.appendChild(item);
        });
        
        lucide.createIcons();
    } catch (e) {
        console.error("Load chats error:", e);
        chatListContainer.innerHTML = '<div class="chat-list-empty">Could not load chats</div>';
    }
}

/**
 * Load a specific chat conversation
 */
async function loadChat(chatId) {
    try {
        var res = await fetch('http://127.0.0.1:8000/get-chat/' + chatId);
        var data = await res.json();
        
        currentChatId = chatId;
        currentMessages = data.messages || [];
        
        // Clear chat display
        chatDisplay.innerHTML = '';
        if (welcomeScreen) welcomeScreen.style.display = 'none';
        
        // Render all messages
        currentMessages.forEach(function(msg) {
            addMessage(msg.content, msg.sender, msg.isHTML || false);
        });
        
        // Update active state in sidebar
        document.querySelectorAll('.chat-list-item').forEach(function(item) {
            item.classList.remove('active');
            if (item.getAttribute('data-id') === chatId) {
                item.classList.add('active');
            }
        });
        
    } catch (e) {
        console.error("Load chat error:", e);
    }
}

/**
 * Create a new chat
 */
async function createNewChat(firstMessage) {
    try {
        var res = await fetch('http://127.0.0.1:8000/create-chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                first_message: firstMessage,
                messages: []
            })
        });
        
        var data = await res.json();
        currentChatId = data.id;
        currentMessages = [];
        
        loadChatList();
        return data.id;
    } catch (e) {
        console.error("Create chat error:", e);
        return null;
    }
}

/**
 * Save messages to current chat
 */
async function saveMessagesToChat(newMessages) {
    if (!currentChatId) return;
    
    try {
        await fetch('http://127.0.0.1:8000/update-chat/' + currentChatId, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                messages: newMessages,
                update_title: currentMessages.length === 0
            })
        });
        
        currentMessages = currentMessages.concat(newMessages);
        loadChatList();
    } catch (e) {
        console.error("Save messages error:", e);
    }
}

/**
 * Show dropdown menu for a chat
 */
var activeDropdown = null;
function showChatMenu(chat, buttonElement) {
    // Remove any existing dropdown
    if (activeDropdown) {
        activeDropdown.remove();
        activeDropdown = null;
    }
    
    var dropdown = document.createElement('div');
    dropdown.className = 'chat-dropdown';
    
    dropdown.innerHTML = 
        '<div class="chat-dropdown-item rename-chat">' +
            '<i data-lucide="edit-3" style="width:14px; height:14px;"></i>' +
            '<span>Rename</span>' +
        '</div>' +
        '<div class="chat-dropdown-item delete">' +
            '<i data-lucide="trash-2" style="width:14px; height:14px;"></i>' +
            '<span>Delete</span>' +
        '</div>';
    
    // Position dropdown
    var rect = buttonElement.getBoundingClientRect();
    dropdown.style.position = 'fixed';
    dropdown.style.top = rect.bottom + 4 + 'px';
    dropdown.style.left = rect.left - 100 + 'px';
    
    document.body.appendChild(dropdown);
    activeDropdown = dropdown;
    lucide.createIcons();
    
    // Rename handler
    dropdown.querySelector('.rename-chat').onclick = function() {
        dropdown.remove();
        activeDropdown = null;
        var newTitle = prompt('Enter new name:', chat.title);
        if (newTitle && newTitle.trim()) {
            renameChat(chat.id, newTitle.trim());
        }
    };
    
    // Delete handler
    dropdown.querySelector('.delete').onclick = function() {
        dropdown.remove();
        activeDropdown = null;
        if (confirm('Delete this chat?')) {
            deleteChat(chat.id);
        }
    };
    
    // Close on outside click
    setTimeout(function() {
        document.addEventListener('click', closeDropdown);
    }, 10);
}

function closeDropdown(e) {
    if (activeDropdown && !activeDropdown.contains(e.target)) {
        activeDropdown.remove();
        activeDropdown = null;
        document.removeEventListener('click', closeDropdown);
    }
}

/**
 * Rename a chat
 */
async function renameChat(chatId, newTitle) {
    try {
        await fetch('http://127.0.0.1:8000/rename-chat/' + chatId, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle })
        });
        loadChatList();
    } catch (e) {
        console.error("Rename chat error:", e);
    }
}

/**
 * Delete a chat
 */
async function deleteChat(chatId) {
    try {
        await fetch('http://127.0.0.1:8000/delete-chat/' + chatId, {
            method: 'DELETE'
        });
        
        // If deleted current chat, reset UI
        if (chatId === currentChatId) {
            currentChatId = null;
            currentMessages = [];
            chatDisplay.innerHTML = '';
            if (welcomeScreen) welcomeScreen.style.display = 'block';
        }
        
        loadChatList();
    } catch (e) {
        console.error("Delete chat error:", e);
    }
}

/**
 * Start a new chat (clear current and prepare for new)
 */
function startNewChat() {
    currentChatId = null;
    currentMessages = [];
    
    // Rebuild the chat display with welcome screen
    chatDisplay.innerHTML = `
        <div class="welcome-container">
            <h1>Hi Samuel,</h1>
            <h2 class="subtitle">Where should we start with your finances today?</h2>
            
            <div class="suggestion-grid">
                <div class="suggestion-card">
                    <p>Analyze my Starbucks spending this week</p>
                    <i data-lucide="coffee"></i>
                </div>
                <div class="suggestion-card">
                    <p>Can I afford a new laptop next month?</p>
                    <i data-lucide="laptop"></i>
                </div>
                <div class="suggestion-card">
                    <p>Summarize my grocery expenses</p>
                    <i data-lucide="shopping-cart"></i>
                </div>
                <div class="suggestion-card">
                    <p>Calculate my remaining monthly budget</p>
                    <i data-lucide="calculator"></i>
                </div>
            </div>
        </div>
    `;
    
    // Re-initialize icons
    lucide.createIcons();
    
    // Remove active state from all chats
    document.querySelectorAll('.chat-list-item').forEach(function(item) {
        item.classList.remove('active');
    });
}

// New Chat button handler
if (newChatBtn) {
    newChatBtn.onclick = startNewChat;
}

// Load chat list on page load
document.addEventListener('DOMContentLoaded', async function() {
    // Note: migrate-history removed - it was creating duplicate chats
    // Goals are now created directly via chat conversation with the AI
    loadChatList();
});

// Initialize the icons
if (window.lucide) {
    lucide.createIcons();
}
