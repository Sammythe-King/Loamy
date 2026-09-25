import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import Logo from "./assets/LoamyLogo.png";
import { GoogleGlyph } from "./Login";
import "./Login.css";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";

// Same Google OAuth config as Login.jsx - reuses the "Connect Gmail"
// redirect URI (/gmail-connect) since that's the only one already
// authorized in the Google Cloud Console.
const GOOGLE_CLIENT_ID = "72400306293-pnjcf2kqtuli55kdn2l5ouq9qjpgc6kt.apps.googleusercontent.com";
const GOOGLE_REDIRECT_URI = window.location.origin + "/gmail-connect";
const GOOGLE_SCOPES = ["openid", "email", "profile"].join(" ");

const SignUp = () => {
    const navigate = useNavigate();
    const [businessName, setBusinessName] = useState("");
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [showPassword, setShowPassword] = useState(false);
    const [error, setError] = useState("");
    const [isLoading, setIsLoading] = useState(false);

    const handleGoogleSignIn = () => {
        const authUrl = `https://accounts.google.com/o/oauth2/v2/auth?` +
            `client_id=${GOOGLE_CLIENT_ID}` +
            `&redirect_uri=${encodeURIComponent(GOOGLE_REDIRECT_URI)}` +
            `&response_type=code` +
            `&scope=${encodeURIComponent(GOOGLE_SCOPES)}` +
            `&state=auth` +
            `&prompt=select_account`;
        window.location.href = authUrl;
    };

    const handleSignUp = async (e) => {
        e.preventDefault();
        setError("");

        if (!businessName.trim() || !email.trim() || !password.trim()) {
            setError("Please fill in all fields.");
            return;
        }

        if (password.length < 6) {
            setError("Password must be at least 6 characters.");
            return;
        }

        setIsLoading(true);

        try {
            const response = await fetch(`${API_URL}/signup`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: businessName,
                    email: email,
                    password: password
                })
            });

            const data = await response.json();

            if (response.ok && data.status === "success") {
                // A brand-new account must never inherit a previous user's cached
                // Gmail connection/data from this browser. Clear it so onboarding
                // starts from a truly disconnected state.
                [
                    "gmail_connected", "gmail_email_data", "gmail_access_token",
                    "gmail_refresh_token", "gmail_email", "loamy_last_sync_at",
                ].forEach((k) => localStorage.removeItem(k));

                // Auto-login after registration
                localStorage.setItem("loamy_session", JSON.stringify({
                    user_id: data.user_id,
                    email: email,
                    name: businessName
                }));

                // Flow: homepage -> signup -> onboarding -> dashboard. Now that
                // the account exists, send the user into onboarding.
                navigate("/onboarding");
            } else {
                // FastAPI returns errors as {"detail": "..."}, not {"message": "..."}.
                // Fall back to the generic text only if the backend sent nothing.
                setError(data.detail || data.message || "Registration failed. Please try again.");
            }
        } catch (err) {
            console.error("Registration error:", err);
            setError("Connection error. Please make sure the backend is running.");
        }

        setIsLoading(false);
    };

    const handleLogin = () => {
        navigate("/");
    };

    return (
        <div className="login-container">
            <div className="login-card">
                <div className="logo-section">
                    <img src={Logo} alt="Loamy Logo" className="login-logo" />
                    <h1>Loamy</h1>
                </div>
                
                <p className="login-subtitle">Create your account and take control of your finances.</p>
                
                {error && (
                    <div className="error-message">
                        {error}
                    </div>
                )}

                <button
                    type="button"
                    className="google-btn"
                    onClick={handleGoogleSignIn}
                    disabled={isLoading}
                >
                    <GoogleGlyph />
                    <span>Continue with Google</span>
                </button>

                <div className="auth-divider">
                    <span>OR</span>
                </div>

                <form onSubmit={handleSignUp}>
                    <div className="form-group">
                        <label>Business Name</label>
                        <input 
                            type="text" 
                            placeholder="Mama Tolu's Kitchen"
                            value={businessName}
                            onChange={(e) => setBusinessName(e.target.value)}
                            disabled={isLoading}
                        />
                    </div>

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
                                placeholder="Create a strong password"
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
                        {isLoading ? "Creating Account..." : "Continue"}
                    </button>
                </form>

                <p className="signup-link">
                    Already have an account?{" "}
                    <span onClick={handleLogin}>Log In</span>
                </p>
            </div>
        </div>
    );
};

export default SignUp;
