import React, { useState, useEffect, useRef } from "react";
import { useNavigate, Link } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { 
    faPlus, faChartPie, faGear, 
    faRightFromBracket, faArrowUp, faPaperclip, faEnvelope,
    faCoffee, faLaptop, faCartShopping, faCalculator,
    faEllipsis, faPen, faTrash, faCheck, faFileInvoiceDollar,
    faFilePdf, faFileWord, faFileExcel, faFileImage, faFileLines
} from "@fortawesome/free-solid-svg-icons";
import Logo from "./assets/loamylogo.png";
import ReviewQueue from "./ReviewQueue.jsx";
import "./ChatPage.css";

const API_URL = "http://127.0.0.1:8000";

// The logged-in user's id. Every per-user request (chats, dashboard, etc.) must
// be scoped to this so data never leaks between accounts on the same browser.
const getSessionUserId = () => {
    try {
        const sess = JSON.parse(localStorage.getItem("loamy_session") || "{}");
        return sess.user_id || "default";
    } catch {
        return "default";
    }
};

// Everything user-specific we cache in localStorage. Cleared on logout so the
// NEXT user who logs in on this browser starts clean and the background sync
// can't re-push the previous user's Gmail data under the new account.
const clearUserStorage = () => {
    [
        "loamy_session", "gmail_connected", "gmail_email_data",
        "gmail_access_token", "gmail_refresh_token", "gmail_email",
        "loamy_last_sync_at", "loamy_onboarding", "loamy_post_oauth_redirect",
    ].forEach((k) => localStorage.removeItem(k));
};

const ChatPage = () => {
    const navigate = useNavigate();
    
    // Session state
    const [currentUser, setCurrentUser] = useState(null);
    
    // Chat state
    const [messages, setMessages] = useState([]);
    const [inputText, setInputText] = useState("");
    const [isLoading, setIsLoading] = useState(false);
    const [showWelcome, setShowWelcome] = useState(true);
    
    // Chat history state
    const [chatList, setChatList] = useState([]);
    // True until the first /get-chats response arrives. Without this, chatList's
    // initial [] rendered "No chats yet" for a moment on every visit, even though
    // the user actually has chats - it just hadn't loaded them yet.
    const [chatsLoading, setChatsLoading] = useState(true);
    const [currentChatId, setCurrentChatId] = useState(null);
    
    // File upload state
    const [pendingFile, setPendingFile] = useState(null);
    const [filePreview, setFilePreview] = useState(null);
    
    // Pending allocations
    const [pendingAllocations, setPendingAllocations] = useState([]);
    
    // Gmail connection state
    const [gmailConnected, setGmailConnected] = useState(false);
    
    // Dropdown state
    const [activeDropdown, setActiveDropdown] = useState(null);

    // Logout confirmation modal
    const [showLogoutModal, setShowLogoutModal] = useState(false);
    
    const chatDisplayRef = useRef(null);
    const fileInputRef = useRef(null);

    // Check session on mount
    useEffect(() => {
        const sessionData = localStorage.getItem("loamy_session");
        if (!sessionData) {
            navigate("/");
            return;
        }
        setCurrentUser(JSON.parse(sessionData));
        setGmailConnected(localStorage.getItem("gmail_connected") === "true");
        loadChatList();
    }, [navigate]);

    // Scroll to bottom when messages change
    useEffect(() => {
        if (chatDisplayRef.current) {
            chatDisplayRef.current.scrollTop = chatDisplayRef.current.scrollHeight;
        }
    }, [messages]);

    // Load chat list from backend, scoped to the logged-in user so one user
    // never sees another user's chats in the sidebar.
    const loadChatList = async () => {
        try {
            const res = await fetch(`${API_URL}/get-chats?user_id=${encodeURIComponent(getSessionUserId())}`);
            const data = await res.json();
            setChatList(data.chats || []);
        } catch (e) {
            console.error("Load chats error:", e);
        } finally {
            setChatsLoading(false);
        }
    };

    // Load a specific chat
    const loadChat = async (chatId) => {
        try {
            const res = await fetch(`${API_URL}/get-chat/${chatId}`);
            const data = await res.json();
            setCurrentChatId(chatId);
            setMessages(data.messages || []);
            setShowWelcome(false);
        } catch (e) {
            console.error("Load chat error:", e);
        }
    };

    // Create new chat
const createNewChat = async (title) => {
        try {
            const res = await fetch(`${API_URL}/create-chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ first_message: title, messages: [], user_id: getSessionUserId() })
            });
            const data = await res.json();
            if (data.status === "success") {
                setCurrentChatId(data.id);
                loadChatList();
                return data.id;
            }
            return null;
        } catch (e) {
            console.error("Create chat error:", e);
            return null;
        }
    };

    // Save messages to current chat
    // Pass the FULL array of messages to save (don't rely on state which may be stale)
    const saveMessagesToChat = async (allMessagesToSave, chatId = null) => {
        const targetChatId = chatId || currentChatId;
        if (!targetChatId) return;
        try {
            await fetch(`${API_URL}/update-chat/${targetChatId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ 
                    messages: allMessagesToSave,
                    update_title: allMessagesToSave.length <= 2
                })
            });
            loadChatList();
        } catch (e) {
            console.error("Save messages error:", e);
        }
    };

    // Generate smart title from message
    const generateSmartTitle = (message) => {
        const text = message.toLowerCase().trim();
        
        if (text.includes("save") || text.includes("goal")) {
            return "Savings Planning";
        }
        if (text.includes("budget")) {
            return "Budget Review";
        }
        if (text.includes("spend")) {
            return "Spending Analysis";
        }
        if (text.includes("afford")) {
            return "Affordability Check";
        }
        
        // Extract key words
        const words = text.split(" ").filter(w => w.length > 3).slice(0, 3);
        if (words.length > 0) {
            return words.map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
        }
        
        return "Financial Consultation";
    };

    // Parse markdown to HTML
    const parseMarkdown = (text) => {
        if (!text) return "";
        
        let html = text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;");
        
        // Headers
        html = html.replace(/^### (.+)$/gm, "<h4>$1</h4>");
        html = html.replace(/^## (.+)$/gm, "<h3>$1</h3>");
        html = html.replace(/^# (.+)$/gm, "<h2>$1</h2>");
        
        // Bold
        html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
        
        // Italic
        html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
        
        // Lists
        html = html.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>");
        
        // Line breaks
        html = html.replace(/\n\n/g, "</p><p>");
        html = html.replace(/\n/g, "<br>");
        
        return html;
    };

    // Handle text chat submission
    const handleSubmit = async (e) => {
        if (e) e.preventDefault();
        
        // Handle file upload
        if (pendingFile) {
            await uploadFile();
            return;
        }
        
        const text = inputText.trim();
        if (!text) return;
        
        // Create chat if none exists
        let chatId = currentChatId;
        if (!chatId) {
            const title = generateSmartTitle(text);
            chatId = await createNewChat(title);
            if (!chatId) return;
        }
        
        // Add user message
        const userMsg = { sender: "user", content: text, isHTML: false };
        const currentMessages = [...messages, userMsg];
        setMessages(currentMessages);
        setInputText("");
        setShowWelcome(false);
        setIsLoading(true);
        
        try {
            // Check for confirmation
            const lowerText = text.toLowerCase();
            const confirmWords = ["yes", "yep", "yeah", "sure", "ok", "okay", "confirm"];
            const isConfirmation = confirmWords.some(w => lowerText.includes(w));
            
            if (isConfirmation && pendingAllocations.length > 0) {
                const allocRes = await fetch(`${API_URL}/allocate-funds`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ allocations: pendingAllocations })
                });
                const allocResult = await allocRes.json();
                
                if (allocResult.status === "success") {
                    const msg = "Done! I've updated your dashboard. Refresh the dashboard to see the changes.";
                    const aiMsg = { sender: "ai", content: msg, isHTML: false };
                    const allMessages = [...currentMessages, aiMsg];
                    setMessages(allMessages);
                    saveMessagesToChat(allMessages, chatId);
                }
                setPendingAllocations([]);
                setIsLoading(false);
                return;
            }
            
            // Send to AI with user_id for Gmail data access
            const sessionData = JSON.parse(localStorage.getItem("loamy_session") || "{}");
            const userId = sessionData.user_id || "default_user";
            
            const response = await fetch(`${API_URL}/chat`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text, user_id: userId })
            });
            
            const data = await response.json();
            const reply = data.reply || "I'm sorry, I couldn't process that request.";
            
            // Check for allocation proposals
            if (reply.includes("update your dashboard")) {
                const parsed = parseAllocationsFromReply(reply);
                if (parsed.length > 0) {
                    setPendingAllocations(parsed);
                }
            }
            
            const aiMsg = { sender: "ai", content: reply, isHTML: false };
            const allMessages = [...currentMessages, aiMsg];
            setMessages(allMessages);
            saveMessagesToChat(allMessages, chatId);
            
        } catch (error) {
            console.error("Chat error:", error);
            const errorMsg = { sender: "ai", content: "Advisor is currently offline.", isHTML: false };
            setMessages(prev => [...prev, errorMsg]);
        }
        
        setIsLoading(false);
    };

    // Parse allocations from AI reply
    const parseAllocationsFromReply = (reply) => {
        const allocations = [];
        const lines = reply.split("\n");
        
        for (const line of lines) {
            const match1 = line.match(/assign\s*\$?([\d,]+\.?\d*)\s*to\s+([A-Za-z][A-Za-z\s]*)/i);
            const match2 = line.match(/^\*?\*?([A-Za-z][A-Za-z\s]*)\*?\*?:\s*Add\s*\$?([\d,]+\.?\d*)/i);
            
            if (match1) {
                allocations.push({ item: match1[2].trim(), amount: parseFloat(match1[1].replace(",", "")) });
            } else if (match2) {
                allocations.push({ item: match2[1].trim(), amount: parseFloat(match2[2].replace(",", "")) });
            }
        }
        
        return allocations;
    };

    // Pick a format-appropriate icon for non-image documents.
    const getFileIcon = (file) => {
        const name = (file?.name || "").toLowerCase();
        const type = (file?.type || "").toLowerCase();
        if (type.includes("pdf") || name.endsWith(".pdf")) return faFilePdf;
        if (type.includes("word") || name.endsWith(".doc") || name.endsWith(".docx")) return faFileWord;
        if (type.includes("sheet") || type.includes("excel") ||
            name.endsWith(".xls") || name.endsWith(".xlsx") || name.endsWith(".csv")) return faFileExcel;
        if (type.startsWith("image/")) return faFileImage;
        return faFileLines;
    };

    // Handle file selection
    const handleFileSelect = (e) => {
        const file = e.target.files[0];
        if (!file) return;
        
        setPendingFile(file);
        
        if (file.type.startsWith("image/")) {
            const reader = new FileReader();
            reader.onload = (e) => setFilePreview(e.target.result);
            reader.readAsDataURL(file);
        } else {
            // Non-image: sentinel value so the UI shows a format icon, not a
            // broken <img>. (The old code pointed at a missing /pdf-icon.svg.)
            setFilePreview("__doc__");
        }
    };

    // Remove pending file
    const removeFile = () => {
        setPendingFile(null);
        setFilePreview(null);
    };

    // Upload file
    const uploadFile = async () => {
        if (!pendingFile) return;
        
        setShowWelcome(false);
        setIsLoading(true);
        
        // Create user message but don't add to state yet
        const userMsg = { sender: "user", content: "Scanning receipt...", isHTML: false };
        
        const formData = new FormData();
        formData.append("file", pendingFile);
        formData.append("user_id", getSessionUserId());
        
        setPendingFile(null);
        setFilePreview(null);
        
        try {
            const response = await fetch(`${API_URL}/upload-artifact`, {
                method: "POST",
                body: formData
            });
            
            const data = await response.json();
            let aiResponse = "";
            let isHTML = false;
            
            if (data.error) {
                aiResponse = "Error: " + data.error;
            } else if (typeof data.analysis === "string") {
                aiResponse = data.analysis;
            } else {
                // Pass currency_info from backend response
                aiResponse = createSummaryTable(data.analysis, data.currency_info);
                isHTML = true;
            }
            
            const aiMsg = { sender: "ai", content: aiResponse, isHTML };
            
            // Handle chat creation
            let targetChatId = currentChatId;
            if (!currentChatId) {
                const vendor = data.analysis?.vendor || "Receipt";
                targetChatId = await createNewChat(`${vendor} Analysis`);
                setCurrentChatId(targetChatId);
            }
            
            // Build complete message array: existing + user message + AI response
            const completeMessages = [...messages, userMsg, aiMsg];
            
            // Update UI state
            setMessages(completeMessages);
            
            // Save to backend
            saveMessagesToChat(completeMessages, targetChatId);
            
        } catch (error) {
            const errorMsg = { sender: "ai", content: "Network error. Is the backend running?", isHTML: false };
            setMessages(prev => [...prev, errorMsg]);
        }
        
        setIsLoading(false);
    };

    // Create summary table HTML
    const createSummaryTable = (data, currencyInfo = null) => {
        if (!data) return "";
        
        // Determine currency symbol
        const currencySymbols = {
            'NGN': '₦', 'USD': '$', 'EUR': '€', 'GBP': '£',
            'ZAR': 'R', 'KES': 'KES', 'GHS': 'GH₵'
        };
        
        // Use currency from API response, default to NGN
        const currency = currencyInfo?.displayed_currency || data.currency || 'NGN';
        const symbol = currencySymbols[currency] || '₦';
        const displayAmount = currencyInfo?.displayed_amount || data.total || 0;
        
        return `
            <div class="summary-container">
                <div class="verification-tag">
                    <strong>Verification Confirmed:</strong> ${data.dpc_logic || ""}
                </div>
                <div class="receipt-details">
                    <p><strong>Vendor:</strong> ${data.vendor || "Unknown"}</p>
                    <p><strong>Date:</strong> ${data.date || "Unknown"}</p>
                    <p><strong>Category:</strong> ${data.category || "Other"}</p>
                    <p class="total-highlight"><strong>Total:</strong> ${symbol}${Number(displayAmount).toLocaleString('en-NG', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</p>
                </div>
            </div>
        `;
    };

    // Start new chat
    const startNewChat = () => {
        setCurrentChatId(null);
        setMessages([]);
        setShowWelcome(true);
        setPendingAllocations([]);
    };

    // Delete chat
    const deleteChat = async (chatId) => {
        try {
            await fetch(`${API_URL}/delete-chat/${chatId}`, { method: "DELETE" });
            if (chatId === currentChatId) {
                startNewChat();
            }
            loadChatList();
        } catch (e) {
            console.error("Delete chat error:", e);
        }
    };

    // Rename chat
    const renameChat = async (chatId, newTitle) => {
        try {
            await fetch(`${API_URL}/rename-chat/${chatId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: newTitle })
            });
            loadChatList();
        } catch (e) {
            console.error("Rename chat error:", e);
        }
    };

    // Logout: open a proper confirmation modal instead of a browser alert.
    const handleLogout = () => setShowLogoutModal(true);

    // Confirmed logout: wipe ALL user-scoped storage (session + Gmail tokens/
    // data) so the next account on this browser starts clean and the background
    // sync can't re-push this user's Gmail data under a different account.
    const confirmLogout = async () => {
        // Purge this user's server-side Gmail blob so nothing they synced on this
        // browser can surface for whoever logs in next. Best-effort: never block
        // logout on it.
        const userId = getSessionUserId();
        try {
            await fetch(`${API_URL}/clear-gmail-data`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ user_id: userId }),
            });
        } catch (e) {
            console.error("Clear gmail on logout failed:", e);
        }
        clearUserStorage();
        setShowLogoutModal(false);
        navigate("/");
    };

    // Suggestion cards
    const suggestions = [
        { text: "Analyze my spending this week", icon: faCoffee },
        { text: "Can I afford a new laptop?", icon: faLaptop },
        { text: "Summarize my grocery expenses", icon: faCartShopping },
        { text: "Calculate my remaining budget", icon: faCalculator }
    ];

    return (
        <div className="chat-app-container">
            {/* Sidebar */}
            <aside className="chat-sidebar">
                <div className="sidebar-top">
                    <div className="logo-section">
                        <img src={Logo} alt="Loamy" className="sidebar-logo" />
                        <span className="logo-text">Loamy</span>
                    </div>
                    <button className="new-chat-btn" onClick={startNewChat}>
                        <FontAwesomeIcon icon={faPlus} />
                        <span>New Chat</span>
                    </button>
                </div>

                <div className="sidebar-menu">
                    <div className="menu-section">
                        <p className="section-label">Tools</p>
                        <Link to="/spending" className="menu-item">
                            <FontAwesomeIcon icon={faChartPie} />
                            <span>Spending Analysis</span>
                        </Link>
                        <Link to="/invoices" className="menu-item">
                            <FontAwesomeIcon icon={faFileInvoiceDollar} />
                            <span>Invoices</span>
                        </Link>
                    </div>

                    <div className="menu-section">
                        <p className="section-label">Your Chats</p>
                        <div className="chat-list">
                            {chatsLoading ? (
                                <div className="chat-list-empty">Loading chats...</div>
                            ) : chatList.length === 0 ? (
                                <div className="chat-list-empty">No chats yet</div>
                            ) : (
                                chatList.map(chat => (
                                    <div 
                                        key={chat.id} 
                                        className={`chat-list-item ${chat.id === currentChatId ? "active" : ""}`}
                                        onClick={() => loadChat(chat.id)}
                                    >
                                        <span className="chat-title">{chat.title}</span>
                                        <button 
                                            className="chat-menu-btn"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                setActiveDropdown(activeDropdown === chat.id ? null : chat.id);
                                            }}
                                        >
                                            <FontAwesomeIcon icon={faEllipsis} />
                                        </button>
                                        {activeDropdown === chat.id && (
                                            <div className="chat-dropdown">
                                                <div 
                                                    className="chat-dropdown-item"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        const newTitle = prompt("Enter new name:", chat.title);
                                                        if (newTitle) renameChat(chat.id, newTitle);
                                                        setActiveDropdown(null);
                                                    }}
                                                >
                                                    <FontAwesomeIcon icon={faPen} />
                                                    <span>Rename</span>
                                                </div>
                                                <div 
                                                    className="chat-dropdown-item delete"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        if (window.confirm("Delete this chat?")) {
                                                            deleteChat(chat.id);
                                                        }
                                                        setActiveDropdown(null);
                                                    }}
                                                >
                                                    <FontAwesomeIcon icon={faTrash} />
                                                    <span>Delete</span>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                ))
                            )}
                        </div>
                    </div>
                </div>

                <div className="sidebar-bottom">
                    <div className="menu-item">
                        <FontAwesomeIcon icon={faGear} />
                        <span>Settings</span>
                    </div>
                    <div className="menu-item logout-btn" onClick={handleLogout}>
                        <FontAwesomeIcon icon={faRightFromBracket} />
                        <span>Logout</span>
                    </div>
                    {currentUser && (
                        <div className="user-profile">
                            <div className="avatar">{currentUser.name?.charAt(0).toUpperCase() || "U"}</div>
                            <span>{currentUser.name || "User"}</span>
                        </div>
                    )}
                </div>
            </aside>

            {/* Main Chat Area */}
            <main className="chat-main">
                <header className="chat-header">
                    <div className="model-selector">
                        <span>Loamy AI Pro</span>
                    </div>
                    <ReviewQueue
                        userId={currentUser?.id || currentUser?.user_id || "default"}
                        userName={currentUser?.name || "there"}
                    />
                </header>

                <div className="chat-content" ref={chatDisplayRef}>
                    {showWelcome ? (
                        <div className="welcome-container">
                            <h1>Hi {currentUser?.name?.split(" ")[0] || "there"},</h1>
                            <h2 className="subtitle">Where should we start with your finances today?</h2>
                            
                            <div className="suggestion-grid">
                                {suggestions.map((s, i) => (
                                    <div 
                                        key={i} 
                                        className="suggestion-card"
                                        onClick={() => {
                                            setInputText(s.text);
                                        }}
                                    >
                                        <p>{s.text}</p>
                                        <FontAwesomeIcon icon={s.icon} />
                                    </div>
                                ))}
                            </div>
                        </div>
                    ) : (
                        <>
                            {messages.map((msg, i) => (
                                <div key={i} className={`message ${msg.sender}-message`}>
                                    {msg.isHTML ? (
                                        <div dangerouslySetInnerHTML={{ __html: msg.content }} />
                                    ) : (
                                        <div dangerouslySetInnerHTML={{ __html: parseMarkdown(msg.content) }} />
                                    )}
                                </div>
                            ))}
                            {isLoading && (
                                <div className="message ai-message">
                                    <div className="typing-indicator">
                                        <span></span><span></span><span></span>
                                    </div>
                                </div>
                            )}
                        </>
                    )}
                </div>

                {/* Input Area */}
                <footer className="input-container">
                    {filePreview && (
                        <div className="file-preview-container">
                            <div className="file-preview">
                                {filePreview.startsWith("data:") ? (
                                    <img src={filePreview || "/placeholder.svg"} alt="Receipt preview" />
                                ) : (
                                    <div className="file-preview-icon" aria-hidden="true">
                                        <FontAwesomeIcon icon={getFileIcon(pendingFile)} />
                                    </div>
                                )}
                                <div className="file-preview-info">
                                    <span>{pendingFile?.name}</span>
                                    <button className="file-remove-btn" onClick={removeFile}>×</button>
                                </div>
                            </div>
                        </div>
                    )}
                    
                    <form className="input-wrapper" onSubmit={handleSubmit}>
                        <label className="icon-btn" title="Upload Receipt">
                            <FontAwesomeIcon icon={faPaperclip} />
                            <input 
                                type="file" 
                                hidden 
                                accept="image/*,.pdf,.doc,.docx,.xls,.xlsx,.csv" 
                                ref={fileInputRef}
                                onChange={handleFileSelect}
                            />
                        </label>
                        <button 
                            type="button"
                            className={`icon-btn gmail-btn ${gmailConnected ? "connected" : ""}`}
                            title={gmailConnected ? "Gmail Connected" : "Connect Gmail"}
                            onClick={() => navigate("/gmail-connect")}
                        >
                            <FontAwesomeIcon icon={faEnvelope} />
                        </button>
                        <input 
                            type="text" 
                            placeholder="Ask your financial advisor..."
                            value={inputText}
                            onChange={(e) => setInputText(e.target.value)}
                            onKeyPress={(e) => e.key === "Enter" && handleSubmit(e)}
                        />
                        <button type="submit" className="send-btn" disabled={isLoading}>
                            <FontAwesomeIcon icon={faArrowUp} />
                        </button>
                    </form>
                    <p className="disclaimer">Loamy AI can make mistakes. Always verify important financial decisions.</p>
                </footer>
            </main>

            {/* Logout confirmation modal */}
            {showLogoutModal && (
                <div
                    className="logout-modal-overlay"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="logout-modal-title"
                    onClick={() => setShowLogoutModal(false)}
                >
                    <div className="logout-modal" onClick={(e) => e.stopPropagation()}>
                        <div className="logout-modal-icon">
                            <FontAwesomeIcon icon={faRightFromBracket} />
                        </div>
                        <h3 id="logout-modal-title">Log out of Loamy?</h3>
                        <p>You&apos;ll need to sign back in to access your dashboard, chats, and financial data.</p>
                        <div className="logout-modal-actions">
                            <button
                                type="button"
                                className="logout-modal-cancel"
                                onClick={() => setShowLogoutModal(false)}
                            >
                                Cancel
                            </button>
                            <button
                                type="button"
                                className="logout-modal-confirm"
                                onClick={confirmLogout}
                            >
                                Log Out
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default ChatPage;
