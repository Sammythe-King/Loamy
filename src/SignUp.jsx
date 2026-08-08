import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import Logo from "./assets/loamylogo.png";
import "./Login.css";

const API_URL = "http://127.0.0.1:8000";

const SignUp = () => {
    const navigate = useNavigate();
    const [businessName, setBusinessName] = useState("");
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [error, setError] = useState("");
    const [isLoading, setIsLoading] = useState(false);

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
                setError(data.message || "Registration failed. Please try again.");
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
                        <input 
                            type="password" 
                            placeholder="Create a strong password"
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
