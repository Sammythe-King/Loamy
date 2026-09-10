import React, { useState, useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
    faArrowLeft, faEnvelope, faEnvelopeOpen, faReceipt, faCalendarCheck,
    faBuilding, faFile, faShieldHalved, faRotate, faPlug
} from "@fortawesome/free-solid-svg-icons";
import "./GmailConnectPage.css";

const API_URL = "http://127.0.0.1:8000";

// Google OAuth Configuration
const GOOGLE_CLIENT_ID = "72400306293-pnjcf2kqtuli55kdn2l5ouq9qjpgc6kt.apps.googleusercontent.com";
const GOOGLE_REDIRECT_URI = window.location.origin + "/gmail-connect";
const GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email"
].join(" ");

const GmailConnectPage = () => {
    const navigate = useNavigate();
    
    // State
    const [isConnected, setIsConnected] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    const [emailsScanned, setEmailsScanned] = useState(0);
    const [transactionsFound, setTransactionsFound] = useState(0);
    const [billsDetected, setBillsDetected] = useState(0);
    const [emailList, setEmailList] = useState([]);

    useEffect(() => {
        const urlParams = new URLSearchParams(window.location.search);
        const code = urlParams.get("code");
        const state = urlParams.get("state");

        // "Continue with Google" on Login/Sign Up reuses this page's redirect
        // URI (it's the only one authorized in Google Cloud Console) and tags
        // its request with state=auth. That request happens BEFORE the user
        // has a session, so it must be handled ahead of the session check
        // below, which would otherwise bounce them straight back to "/".
        if (code && state === "auth") {
            window.history.replaceState({}, document.title, "/gmail-connect");
            handleAuthLoginCallback(code);
            return;
        }

        // Check session
        const sessionData = localStorage.getItem("loamy_session");
        if (!sessionData) {
            navigate("/");
            return;
        }

        if (code) {
            handleOAuthCallback(code);
            // Clear the URL params
            window.history.replaceState({}, document.title, "/gmail-connect");
        } else {
            // Check Gmail connection status
            const gmailConnected = localStorage.getItem("gmail_connected") === "true";
            setIsConnected(gmailConnected);

            if (gmailConnected) {
                loadEmailData();
            }
        }
    }, [navigate]);

    // Handles the "Continue with Google" sign-in/sign-up flow (as opposed to
    // the "Connect Gmail for bank sync" flow this page normally handles).
    const handleAuthLoginCallback = async (code) => {
        setIsLoading(true);
        try {
            const response = await fetch(`${API_URL}/google-auth`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ code, redirect_uri: GOOGLE_REDIRECT_URI })
            });
            const data = await response.json();

            if (data.status === "success") {
                // If a DIFFERENT user is signing in on this browser, drop the
                // previous user's cached Gmail connection so they don't see it.
                const prev = JSON.parse(localStorage.getItem("loamy_session") || "{}");
                const hasCachedGmail =
                    localStorage.getItem("gmail_access_token") ||
                    localStorage.getItem("gmail_email_data");
                if (hasCachedGmail && prev.user_id !== data.user_id) {
                    [
                        "gmail_connected", "gmail_email_data", "gmail_access_token",
                        "gmail_refresh_token", "gmail_email", "loamy_last_sync_at",
                    ].forEach((k) => localStorage.removeItem(k));
                }

                localStorage.setItem("loamy_session", JSON.stringify({
                    user_id: data.user_id,
                    email: data.email,
                    name: data.name || data.email?.split("@")[0]
                }));

                // Brand-new Google accounts haven't been through onboarding yet;
                // returning accounts go straight to chat.
                navigate(data.is_new_user ? "/onboarding" : "/chat");
            } else {
                navigate("/?google_error=" + encodeURIComponent(data.error || "Google sign-in failed. Please try again."));
            }
        } catch (error) {
            console.error("Google sign-in error:", error);
            navigate("/?google_error=" + encodeURIComponent("Connection error. Please make sure the backend is running."));
        }
        setIsLoading(false);
    };

    const handleOAuthCallback = async (code) => {
        setIsLoading(true);
        try {
            const response = await fetch(`${API_URL}/gmail/exchange-token`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ 
                    code: code,
                    redirect_uri: GOOGLE_REDIRECT_URI
                })
            });
            
            const data = await response.json();
            
            if (data.status === "success") {
                localStorage.setItem("gmail_connected", "true");
                localStorage.setItem("gmail_access_token", data.access_token);
                localStorage.setItem("gmail_email", data.email || "");
                if (data.refresh_token) {
                    localStorage.setItem("gmail_refresh_token", data.refresh_token);
                    // Persist the refresh token on the backend so it can auto-sync
                    // bank data server-side without a live browser token.
                    try {
                        const sess = JSON.parse(localStorage.getItem("loamy_session") || "{}");
                        if (sess.user_id) {
                            await fetch(`${API_URL}/gmail/store-credentials`, {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ user_id: sess.user_id, refresh_token: data.refresh_token })
                            });
                        }
                    } catch (e) {
                        console.error("[v0] Failed to store Gmail credentials on backend:", e);
                    }
                }
                
                setIsConnected(true);
                
                // Fetch emails immediately
                await fetchEmails(data.access_token);

                // If we arrived here from onboarding, continue to the intended
                // destination (the dashboard) now that Gmail is truly connected
                // and the first sync has run.
                const postRedirect = localStorage.getItem("loamy_post_oauth_redirect");
                if (postRedirect) {
                    localStorage.removeItem("loamy_post_oauth_redirect");
                    setIsLoading(false);
                    navigate(postRedirect);
                    return;
                }
            } else {
                alert("Failed to connect Gmail: " + (data.error || "Unknown error"));
            }
        } catch (error) {
            console.error("OAuth callback error:", error);
            alert("Error connecting Gmail. Please try again.");
        }
        setIsLoading(false);
    };

    // Helper function to refresh expired token
    const refreshAccessToken = async () => {
        const refreshToken = localStorage.getItem("gmail_refresh_token");
        if (!refreshToken) return null;
        
        try {
            const response = await fetch(`${API_URL}/gmail/refresh-token`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ refresh_token: refreshToken })
            });
            
            const data = await response.json();
            if (data.status === "success" && data.access_token) {
                localStorage.setItem("gmail_access_token", data.access_token);
                return data.access_token;
            }
        } catch (error) {
            console.error("Token refresh failed:", error);
        }
        return null;
    };

    const fetchEmails = async (accessToken) => {
        try {
            // Pass user_id so the backend tracks this user's delta-sync bookmark.
            const sessionData = JSON.parse(localStorage.getItem("loamy_session") || "{}");
            const userId = sessionData.user_id || "default";
            const response = await fetch(`${API_URL}/gmail/fetch-emails`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ access_token: accessToken, user_id: userId })
            });
            
            const data = await response.json();
            
            // Check if token expired - try to refresh
            if (data.requires_reauth || data.error === "token_expired" || 
                (data.status === "error" && (data.error?.includes("401") || data.error?.includes("UNAUTHENTICATED")))) {
                const newToken = await refreshAccessToken();
                if (newToken) {
                    // Retry with new token
                    return fetchEmails(newToken);
                } else {
                    // Refresh failed - user needs to reconnect
                    alert("Gmail session expired. Please reconnect your Gmail account.");
                    handleDisconnect();
                    return;
                }
            }
            
            if (data.status === "success") {
                const emailData = {
                    emails: data.emails,
                    stats: data.stats,
                    total_scanned: data.stats?.emailsScanned || 0
                };
                localStorage.setItem("gmail_email_data", JSON.stringify(emailData));
                displayEmailData(emailData);
                
                // Sync Gmail data to database so AI has access
                await syncGmailDataToDatabase(data.emails, data.stats);
            }
        } catch (error) {
            console.error("Error fetching emails:", error);
        }
    };
    
    const syncGmailDataToDatabase = async (emails, stats) => {
        try {
            const sessionData = JSON.parse(localStorage.getItem("loamy_session") || "{}");
            const userId = sessionData.user_id;
            
            if (!userId) return;
            
            await fetch(`${API_URL}/sync-gmail-data`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    user_id: userId,
                    emails: emails,
                    stats: stats
                })
            });
            
            console.log("Gmail data synced to database for AI access");
        } catch (error) {
            console.error("Error syncing Gmail data:", error);
        }
    };

    const loadEmailData = async () => {
        const cachedData = localStorage.getItem("gmail_email_data");
        if (cachedData) {
            const parsed = JSON.parse(cachedData);
            displayEmailData(parsed);
        }
    };

    const displayEmailData = (data) => {
        if (data && data.emails) {
            setEmailsScanned(data.total_scanned || data.stats?.emailsScanned || data.emails.length);
            
            // Count transactions and bills (including bank alerts)
            let transactions = 0;
            let bills = 0;
            
            data.emails.forEach(email => {
                // Bank alerts and receipts count as transactions
                if (email.type === "receipt" || email.type === "payment" || 
                    email.type === "bank_alert" || email.type === "bank_debit" || 
                    email.type === "bank_credit" || email.is_bank_alert) {
                    transactions++;
                }
                // Bills and subscriptions
                if (email.type === "bill" || email.type === "subscription") {
                    bills++;
                }
            });
            
            setTransactionsFound(transactions);
            setBillsDetected(bills);
            setEmailList(data.emails.slice(0, 20));
        }
    };

    const handleConnect = () => {
        // Redirect directly to Google OAuth
        const authUrl = `https://accounts.google.com/o/oauth2/v2/auth?` +
            `client_id=${GOOGLE_CLIENT_ID}` +
            `&redirect_uri=${encodeURIComponent(GOOGLE_REDIRECT_URI)}` +
            `&response_type=code` +
            `&scope=${encodeURIComponent(GOOGLE_SCOPES)}` +
            `&access_type=offline` +
            `&prompt=consent`;
        
        window.location.href = authUrl;
    };

    const handleRefresh = async () => {
        setIsLoading(true);
        
        const accessToken = localStorage.getItem("gmail_access_token");
        if (!accessToken) {
            alert("Not connected to Gmail. Please reconnect.");
            setIsLoading(false);
            return;
        }
        
        try {
            const response = await fetch(`${API_URL}/gmail/fetch-emails`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ access_token: accessToken })
            });
            
            const data = await response.json();
            
            if (data.status === "success" && data.emails) {
                const emailData = {
                    emails: data.emails,
                    stats: data.stats,
                    total_scanned: data.stats?.emailsScanned || 0
                };
                localStorage.setItem("gmail_email_data", JSON.stringify(emailData));
                displayEmailData(emailData);
                
                // Sync to database for AI access
                await syncGmailDataToDatabase(data.emails, data.stats);
            }
        } catch (error) {
            console.error("Error fetching emails:", error);
            
            // Show cached data if available
            const cachedData = localStorage.getItem("gmail_email_data");
            if (cachedData) {
                displayEmailData(JSON.parse(cachedData));
                alert("Network error - showing cached data. Try again when connection is stable.");
            } else {
                alert("Network error. Please check your connection and try again.");
            }
        }
        
        setIsLoading(false);
    };

    const handleDisconnect = () => {
        if (window.confirm("Are you sure you want to disconnect Gmail?")) {
            localStorage.removeItem("gmail_connected");
            localStorage.removeItem("gmail_email_data");
            setIsConnected(false);
            setEmailsScanned(0);
            setTransactionsFound(0);
            setBillsDetected(0);
            setEmailList([]);
        }
    };

    const getEmailTypeLabel = (type) => {
        const labels = {
            receipt: "Receipt",
            payment: "Payment",
            bill: "Bill",
            subscription: "Subscription",
            bank: "Bank Alert",
            bank_alert: "Bank Alert",
            bank_debit: "Debit",
            bank_credit: "Credit",
            invoice: "Invoice"
        };
        return labels[type] || "Email";
    };
    
    // Currency symbol helper - supports global currencies
    const getCurrencySymbol = (currency) => {
        const symbols = {
            NGN: "₦",
            USD: "$",
            EUR: "€",
            GBP: "£",
            ZAR: "R",
            KES: "KSh",
            MUR: "Rs",
            INR: "₹",
            GHS: "GH₵",
            XOF: "CFA",
            XAF: "FCFA",
            AED: "د.إ",
            CAD: "C$",
            AUD: "A$",
            JPY: "¥",
            CNY: "¥"
        };
        return symbols[currency] || currency || "$";
    };

    const getEmailTypeClass = (type) => {
        const classes = {
            receipt: "type-receipt",
            payment: "type-payment",
            bill: "type-bill",
            subscription: "type-subscription",
            bank: "type-bank",
            invoice: "type-invoice"
        };
        return classes[type] || "";
    };

    const features = [
        {
            icon: faReceipt,
            title: "Payment Receipts",
            description: "Automatically detect purchase confirmations from Amazon, PayPal, and more"
        },
        {
            icon: faCalendarCheck,
            title: "Subscription Alerts",
            description: "Track recurring payments like Netflix, Spotify, and gym memberships"
        },
        {
            icon: faBuilding,
            title: "Bank Notifications",
            description: "Read transaction alerts from your bank for real-time tracking"
        },
        {
            icon: faFile,
            title: "Bill Reminders",
            description: "Detect utility bills, rent reminders, and payment due dates"
        }
    ];

    return (
        <div className="gmail-container">
            {/* Header */}
            <header className="gmail-header">
                <Link to="/chat" className="back-btn">
                    <FontAwesomeIcon icon={faArrowLeft} />
                    <span>Back to Chat</span>
                </Link>
                <h1>Gmail Integration</h1>
            </header>

            {/* Main Content */}
            <main className="gmail-content">
                {/* Connection Status Card */}
                <div className="status-card">
                    <div className={`status-icon ${isConnected ? "connected" : "disconnected"}`}>
                        <FontAwesomeIcon icon={isConnected ? faEnvelopeOpen : faEnvelope} />
                    </div>
                    <h2>{isConnected ? "Gmail Connected" : "Gmail Not Connected"}</h2>
                    <p>
                        {isConnected 
                            ? "Your Gmail is connected. Loamy AI can now analyze your financial emails."
                            : "Connect your Gmail to let Loamy AI analyze your bills, subscriptions, and payment receipts."
                        }
                    </p>
                    
                    {!isConnected && (
                        <button className="connect-btn" onClick={handleConnect}>
                            <img 
                                src="https://www.google.com/favicon.ico" 
                                alt="Google" 
                                className="google-icon" 
                            />
                            <span>Connect with Google</span>
                        </button>
                    )}
                </div>

                {/* Features Section */}
                {!isConnected && (
                    <div className="features-section">
                        <h3>What Loamy AI Can Access</h3>
                        <div className="features-grid">
                            {features.map((feature, i) => (
                                <div key={i} className="feature-card">
                                    <div className="feature-icon">
                                        <FontAwesomeIcon icon={feature.icon} />
                                    </div>
                                    <h4>{feature.title}</h4>
                                    <p>{feature.description}</p>
                                </div>
                            ))}
                        </div>
                    </div>
                )}

                {/* Connected State */}
                {isConnected && (
                    <div className="connected-section">
                        <h3>Synced Email Data</h3>
                        <div className="sync-stats">
                            <div className="sync-stat">
                                <span className="stat-number">{emailsScanned}</span>
                                <span className="stat-label">Emails Scanned</span>
                            </div>
                            <div className="sync-stat">
                                <span className="stat-number">{transactionsFound}</span>
                                <span className="stat-label">Transactions Found</span>
                            </div>
                            <div className="sync-stat">
                                <span className="stat-number">{billsDetected}</span>
                                <span className="stat-label">Bills Detected</span>
                            </div>
                        </div>

                        {/* Email Data List */}
                        <div className="email-data-section">
                            <div className="section-header">
                                <h4>Recent Financial Emails</h4>
                                <button 
                                    className={`refresh-btn ${isLoading ? "spinning" : ""}`}
                                    onClick={handleRefresh}
                                    disabled={isLoading}
                                >
                                    <FontAwesomeIcon icon={faRotate} />
                                    <span>{isLoading ? "Refreshing..." : "Refresh"}</span>
                                </button>
                            </div>
                            
                            <div className="email-list">
                                {emailList.length === 0 ? (
                                    <div className="email-empty">
                                        <FontAwesomeIcon icon={faEnvelope} />
                                        <p>No financial emails found yet. Click refresh to scan.</p>
                                    </div>
                                ) : (
                                    emailList.map((email, i) => (
                                        <div key={i} className="email-item">
                                            <div className="email-info">
                                                <span className="email-sender">{email.sender}</span>
                                                <span className="email-subject">{email.subject}</span>
                                            </div>
                                            <div className="email-meta">
                                                <span className={`email-type ${getEmailTypeClass(email.type)}`}>
                                                    {getEmailTypeLabel(email.type)}
                                                </span>
                                                {email.amount && (
                                                    <span className="email-amount">
                                                        {getCurrencySymbol(email.currency)}{email.amount.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
                                                    </span>
                                                )}
                                                <span className="email-date">{email.date}</span>
                                            </div>
                                        </div>
                                    ))
                                )}
                            </div>
                        </div>

                        {/* Disconnect Button */}
                        <button className="disconnect-btn" onClick={handleDisconnect}>
                            <FontAwesomeIcon icon={faPlug} />
                            <span>Disconnect Gmail</span>
                        </button>
                    </div>
                )}

                {/* Privacy Notice */}
                <div className="privacy-notice">
                    <FontAwesomeIcon icon={faShieldHalved} />
                    <div>
                        <h4>Your Privacy is Protected</h4>
                        <p>
                            Loamy AI only reads emails related to finances (receipts, bills, bank alerts). 
                            We never access personal conversations or store your email content on our servers.
                        </p>
                    </div>
                </div>
            </main>
        </div>
    );
};

export default GmailConnectPage;
