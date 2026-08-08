import React, { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import Logo from "./assets/loamylogo.png";
import "./Login.css";

const API_URL = "http://127.0.0.1:8000";

const Login = () => {
    const navigate = useNavigate();
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [error, setError] = useState("");
    const [isLoading, setIsLoading] = useState(false);

    // Check if already logged in
    useEffect(() => {
        const session = localStorage.getItem("loamy_session");
        if (session) {
            navigate("/chat");
        }
    }, [navigate]);

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
                // If a DIFFERENT user is logging in on this browser, drop the
                // previous user's cached Gmail connection/data so they don't see
                // it. A returning user (same id) keeps their cached tokens.
                try {
                    const prev = JSON.parse(localStorage.getItem("loamy_session") || "{}");
                    if (prev.user_id && prev.user_id !== data.user_id) {
                        [
                            "gmail_connected", "gmail_email_data", "gmail_access_token",
                            "gmail_refresh_token", "gmail_email", "loamy_last_sync_at",
                        ].forEach((k) => localStorage.removeItem(k));
                    }
                } catch (_) {}

                // Store session data
                localStorage.setItem("loamy_session", JSON.stringify({
                    user_id: data.user_id,
                    email: data.email,
                    name: data.name || email.split("@")[0]
                }));
                navigate("/chat");
            } else {
                setError(data.message || "Invalid email or password.");
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
                        <input 
                            type="password" 
                            placeholder="Enter your password"
                            value={password}
                            onChange={(e) => setPassword(e.target.value)}
                            disabled={isLoading}
                        />
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
