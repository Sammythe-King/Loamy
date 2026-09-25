import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import Logo from "./assets/loamylogo.png";
import "./Login.css";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

// Google OAuth Configuration - reuses the SAME redirect URI as the existing
// "Connect Gmail" feature (/gmail-connect) because that's the only URI
// already authorized in the Google Cloud Console. GmailConnectPage tells
// this sign-in flow apart from its normal Gmail-connect flow via state=auth.
const GOOGLE_CLIENT_ID = "72400306293-pnjcf2kqtuli55kdn2l5ouq9qjpgc6kt.apps.googleusercontent.com";
const GOOGLE_REDIRECT_URI = window.location.origin + "/gmail-connect";
const GOOGLE_SCOPES = ["openid", "email", "profile"].join(" ");

// Official multi-color Google "G" mark for the sign-in button.
export const GoogleGlyph = () => (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
        <path fill="#FFC107" d="M43.611,20.083H42V20H24v8h11.303c-1.649,4.657-6.08,8-11.303,8c-6.627,0-12-5.373-12-12
            c0-6.627,5.373-12,12-12c3.059,0,5.842,1.154,7.961,3.039l5.657-5.657C34.046,6.053,29.268,4,24,4C12.955,4,4,12.955,4,24
            c0,11.045,8.955,20,20,20c11.045,0,20-8.955,20-20C44,22.659,43.862,21.35,43.611,20.083z" />
        <path fill="#FF3D00" d="M6.306,14.691l6.571,4.819C14.655,15.108,18.961,12,24,12c3.059,0,5.842,1.154,7.961,3.039
            l5.657-5.657C34.046,6.053,29.268,4,24,4C16.318,4,9.656,8.337,6.306,14.691z" />
        <path fill="#4CAF50" d="M24,44c5.166,0,9.86-1.977,13.409-5.192l-6.19-5.238C29.211,35.091,26.715,36,24,36
            c-5.202,0-9.619-3.317-11.283-7.946l-6.522,5.025C9.505,39.556,16.227,44,24,44z" />
        <path fill="#1976D2" d="M43.611,20.083H42V20H24v8h11.303c-0.792,2.237-2.231,4.166-4.087,5.571
            c0.001-0.001,0.002-0.001,0.003-0.002l6.19,5.238C36.971,39.205,44,34,44,24C44,22.659,43.862,21.35,43.611,20.083z" />
    </svg>
);

const Login = () => {
    const navigate = useNavigate();
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [showPassword, setShowPassword] = useState(false);
    const [error, setError] = useState("");
    const [isLoading, setIsLoading] = useState(false);
    const [isGoogleLoading, setIsGoogleLoading] = useState(false);

    const finishLogin = (data, fallbackName) => {
        // If a DIFFERENT user is logging in on this browser, drop the
        // previous user's cached Gmail connection/data so they don't see it.
        try {
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
        } catch (_) {}

        localStorage.setItem("loamy_session", JSON.stringify({
            user_id: data.user_id,
            email: data.email,
            name: data.name || fallbackName
        }));

        // Brand-new Google accounts haven't been through onboarding yet;
        // returning accounts go straight to chat, same as email/password login.
        navigate(data.is_new_user ? "/onboarding" : "/chat");
    };

    // Check if already logged in, or if GmailConnectPage bounced us back here
    // after a failed "Continue with Google" attempt (it does the actual token
    // exchange since /gmail-connect is the only redirect URI Google has on file).
    useEffect(() => {
        const urlParams = new URLSearchParams(window.location.search);
        const googleError = urlParams.get("google_error");
        if (googleError) {
            window.history.replaceState({}, document.title, "/");
            setError(googleError);
            return;
        }

        const session = localStorage.getItem("loamy_session");
        if (session) {
            navigate("/chat");
        }
    }, [navigate]);

    const handleGoogleSignIn = () => {
        setIsGoogleLoading(true);
        const authUrl = `https://accounts.google.com/o/oauth2/v2/auth?` +
            `client_id=${GOOGLE_CLIENT_ID}` +
            `&redirect_uri=${encodeURIComponent(GOOGLE_REDIRECT_URI)}` +
            `&response_type=code` +
            `&scope=${encodeURIComponent(GOOGLE_SCOPES)}` +
            `&state=auth` +
            `&prompt=select_account`;
        window.location.href = authUrl;
    };

    const handleLogin = async (e) => {
        e.preventDefault();
        setError("");
        
        if (!email.trim() || !password.trim()) {
            setError("Please enter both email and password.");
            return;
        }

        setIsLoading(true);

        try {
            const response = await fetch(`${API_URL}/login`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ email, password })
            });

            const data = await response.json();

            if (response.ok && data.status === "success") {
                // Plain email/password accounts have already been through
                // onboarding by definition, so this always goes to chat.
                finishLogin({ ...data, is_new_user: false }, email.split("@")[0]);
            } else {
                // FastAPI returns errors as {"detail": "..."}, not {"message": "..."}.
                // Fall back to the generic text only if the backend sent nothing.
                setError(data.detail || data.message || "Invalid email or password.");
            }
        } catch (err) {
            console.error("Login error:", err);
            setError("Connection error. Please make sure the backend is running.");
        }

        setIsLoading(false);
    };

    const handleSignUp = () => {
        // Flow: homepage -> signup -> onboarding -> dashboard.
        navigate("/signup");
    };

    return (
        <div className="login-container">
            <div className="login-card">
                <div className="logo-section">
                    <img src={Logo} alt="Loamy Logo" className="login-logo" />
                    <h1>Loamy</h1>
                </div>
                
                <p className="login-subtitle">Welcome back. Login to your account.</p>
                
                {error && (
                    <div className="error-message">
                        {error}
                    </div>
                )}

                <button
                    type="button"
                    className="google-btn"
                    onClick={handleGoogleSignIn}
                    disabled={isGoogleLoading || isLoading}
                >
                    <GoogleGlyph />
                    <span>{isGoogleLoading ? "Signing you in..." : "Continue with Google"}</span>
                </button>

                <div className="auth-divider">
                    <span>OR</span>
                </div>

                <form onSubmit={handleLogin}>
                    <div className="form-group">
                        <label>Email</label>
                        <input 
                            type="email" 
                            placeholder="your@business.com"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            disabled={isLoading}
                        />
                    </div>

                    <div className="form-group">
                        <label>Password</label>
                        <div className="password-input-wrapper">
                            <input
                                type={showPassword ? "text" : "password"}
                                placeholder="Enter your password"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                                disabled={isLoading}
                            />
                            <button
                                type="button"
                                className="password-toggle-btn"
                                onClick={() => setShowPassword((v) => !v)}
                                aria-label={showPassword ? "Hide password" : "Show password"}
                                tabIndex={-1}
                            >
                                {showPassword ? (
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                                        <path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-5.5 0-9.5-4-11-8 .78-1.85 2.05-3.68 3.68-5.1M9.9 4.24A10.94 10.94 0 0 1 12 4c5.5 0 9.5 4 11 8-.46 1.09-1.06 2.14-1.8 3.08" />
                                        <path d="M9.5 9.5a3 3 0 1 0 4.24 4.24" />
                                        <line x1="2" y1="2" x2="22" y2="22" />
                                    </svg>
                                ) : (
                                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                                        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z" />
                                        <circle cx="12" cy="12" r="3" />
                                    </svg>
                                )}
                            </button>
                        </div>
                    </div>

                    <button 
                        type="submit" 
                        className="login-btn"
                        disabled={isLoading}
                    >
                        {isLoading ? "Logging in..." : "Log In"}
                    </button>
                </form>

                <p className="signup-link">
                    Don&apos;t have an account?{" "}
                    <span onClick={handleSignUp}>Sign Up</span>
                </p>
            </div>
        </div>
    );
};

export default Login;
