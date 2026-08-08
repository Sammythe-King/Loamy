import { useState, useEffect } from "react";
import "./OnboardingWizard.css";

const API_URL = "http://127.0.0.1:8000";

// Google OAuth config — MUST match GmailConnectPage so the same redirect URI is
// whitelisted in the Google console. Onboarding sends the user through the exact
// same real OAuth flow; the callback is handled on /gmail-connect.
const GOOGLE_CLIENT_ID = "72400306293-pnjcf2kqtuli55kdn2l5ouq9qjpgc6kt.apps.googleusercontent.com";
const GOOGLE_REDIRECT_URI = window.location.origin + "/gmail-connect";
const GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
].join(" ");

// Step 1 options. "Other" is special: it reveals a free-text input so the user
// can type a profession category that isn't listed.
const BUSINESS_TYPES = [
    { id: "Food / Bakery", label: "Food / Bakery", desc: "Cafés, restaurants, catering" },
    { id: "Selling Products", label: "Selling Products", desc: "Retail, e-commerce, resellers" },
    { id: "Freelancer / Creative", label: "Freelancer / Creative", desc: "Design, media, consulting" },
    { id: "Tech / Agency", label: "Tech / Agency", desc: "Startups, software, services" },
    { id: "Other", label: "Other", desc: "Type your own category" },
];

const COUNTRIES = [
    "Nigeria", "Ghana", "Kenya", "South Africa", "United States",
    "United Kingdom", "Canada", "India", "Egypt", "Tanzania", "Uganda", "Rwanda",
];

const LOADER_LINES = [
    "Securely connecting to your bank inbox...",
    "Reading your latest transactions...",
    "Building your financial dashboard...",
];

// Cycling status lines shown WHILE banks load. These create a sense of forward
// motion so the wait feels productive instead of frictional. {country} is
// swapped for the chosen country at runtime.
const BANK_LOADER_LINES = [
    "Scanning banks in {country}...",
    "Gathering local & digital banks...",
    "Matching secure email providers...",
    "Almost ready...",
];

export default function OnboardingWizard() {
    const [step, setStep] = useState(1);

    // Step 1
    const [businessType, setBusinessType] = useState("");
    const [showCustom, setShowCustom] = useState(false);
    const [customType, setCustomType] = useState("");

    // Step 2
    const [country, setCountry] = useState("");
    const [banks, setBanks] = useState([]);
    const [banksLoading, setBanksLoading] = useState(false);
    const [bankQuery, setBankQuery] = useState("");
    const [selectedBank, setSelectedBank] = useState(null);
    const [showSuggestions, setShowSuggestions] = useState(false);

    // Shared
    const [error, setError] = useState("");
    const [loaderIndex, setLoaderIndex] = useState(0);
    const [bankLoaderIndex, setBankLoaderIndex] = useState(0);

    // ----- Step 1 handlers -----
    const handlePickType = (type) => {
        setError("");
        if (type.id === "Other") {
            setShowCustom(true);
            setBusinessType("Other");
            return;
        }
        setShowCustom(false);
        setBusinessType(type.id);
        setStep(2);
    };

    const handleCustomContinue = () => {
        if (!customType.trim()) {
            setError("Please type your profession category.");
            return;
        }
        setBusinessType(customType.trim());
        setStep(2);
    };

    // ----- Step 2 handlers -----
    const handleCountryChange = async (value) => {
        setCountry(value);
        setSelectedBank(null);
        setBankQuery("");
        setBanks([]);
        if (!value) return;
        setBanksLoading(true);
        setError("");
        try {
            const res = await fetch(`${API_URL}/api/onboarding/banks?country=${encodeURIComponent(value)}`);
            const json = await res.json();
            if (json.status === "success") {
                setBanks(json.data.banks || []);
            } else {
                setError(json.error || "Could not load banks.");
            }
        } catch (err) {
            console.error("[v0] Onboarding: bank fetch failed:", err);
            setError("Could not load banks. Check your connection.");
        } finally {
            setBanksLoading(false);
        }
    };

    const filteredBanks = bankQuery.trim()
        ? banks.filter((b) => b.name.toLowerCase().includes(bankQuery.trim().toLowerCase()))
        : banks;

    const handlePickBank = (bank) => {
        setSelectedBank(bank);
        setBankQuery(bank.name);
        setShowSuggestions(false);
    };

    // ----- Step 3 -> connect -----
    // Flow is homepage -> signup -> onboarding -> dashboard, so by the time we
    // reach here the user already has an account. We commit their profile with
    // the real user_id, then run the loader and drop them on the dashboard.
    const handleConnectGmail = async () => {
        const profile = {
            business_type: businessType,
            country,
            selected_bank: selectedBank ? selectedBank.name : "",
            sender_domains: selectedBank ? selectedBank.sender_domains || [] : [],
        };

        const session = JSON.parse(localStorage.getItem("loamy_session") || "{}");

        // 1. Commit the collected profile FIRST (this is what personalizes the
        //    app). We await it so the profile is saved before we leave the page
        //    for Google's consent screen.
        if (session.user_id) {
            try {
                await fetch(`${API_URL}/api/onboarding/submit`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ user_id: session.user_id, ...profile }),
                });
            } catch (err) {
                console.error("[v0] Onboarding: submit failed:", err);
            }
        }

        // 2. Show the connecting loader, then launch the REAL Google OAuth flow.
        //    Previously this just navigated to the dashboard without ever
        //    connecting Gmail, which is why Gmail stayed "Not Connected". We drop
        //    a breadcrumb so that after the OAuth callback finishes on
        //    /gmail-connect, the user lands on the dashboard instead of staying
        //    on the Gmail page.
        setStep(4);
        localStorage.setItem("loamy_post_oauth_redirect", "/dashboard");

        const authUrl =
            `https://accounts.google.com/o/oauth2/v2/auth?` +
            `client_id=${GOOGLE_CLIENT_ID}` +
            `&redirect_uri=${encodeURIComponent(GOOGLE_REDIRECT_URI)}` +
            `&response_type=code` +
            `&scope=${encodeURIComponent(GOOGLE_SCOPES)}` +
            `&access_type=offline` +
            `&prompt=consent`;

        window.location.href = authUrl;
    };

    // Loader text cycling on step 4
    useEffect(() => {
        if (step !== 4) return;
        const t = setInterval(() => {
            setLoaderIndex((i) => (i + 1) % LOADER_LINES.length);
        }, 1000);
        return () => clearInterval(t);
    }, [step]);

    // Cycle the bank-loading status lines while banks are being fetched. The
    // first three lines flip at quick, equal intervals so it feels snappy, then
    // it stops on the last line ("Almost ready...") which dwells the longest,
    // holding until the fetch actually completes.
    useEffect(() => {
        if (!banksLoading) return;
        setBankLoaderIndex(0);
        const t = setInterval(() => {
            setBankLoaderIndex((i) => Math.min(i + 1, BANK_LOADER_LINES.length - 1));
        }, 550);
        return () => clearInterval(t);
    }, [banksLoading]);

    const bankLoaderText = BANK_LOADER_LINES[bankLoaderIndex].replace(
        "{country}",
        country || "your area"
    );

    if (step === 4) {
        return (
            <div className="onb-loader">
                <div className="onb-loader-spinner" />
                <p className="onb-loader-text">{LOADER_LINES[loaderIndex]}</p>
            </div>
        );
    }

    return (
        <div className="onb-container">
            <div className="onb-card">
                <div className="onb-progress">
                    <span className={`onb-dot ${step >= 1 ? "active" : ""}`} />
                    <span className={`onb-dot ${step >= 2 ? "active" : ""}`} />
                    <span className={`onb-dot ${step >= 3 ? "active" : ""}`} />
                </div>

                {/* STEP 1 */}
                {step === 1 && (
                    <div className="onb-step" key="step1">
                        <h1 className="onb-title">What do you do?</h1>
                        <p className="onb-sub">Pick the one that fits you best.</p>

                        <div className="onb-grid">
                            {BUSINESS_TYPES.map((type) => (
                                <button
                                    key={type.id}
                                    type="button"
                                    className={`onb-bento ${businessType === type.id ? "selected" : ""} ${type.id === "Other" ? "onb-bento-wide" : ""}`}
                                    onClick={() => handlePickType(type)}
                                >
                                    <span className="onb-bento-label">{type.label}</span>
                                    <span className="onb-bento-desc">{type.desc}</span>
                                </button>
                            ))}
                        </div>

                        {showCustom && (
                            <div className="onb-custom-block">
                                <label className="onb-label" htmlFor="customType">
                                    Tell us what you do
                                </label>
                                <input
                                    id="customType"
                                    className="onb-input"
                                    placeholder="e.g. Photographer, Real Estate, Logistics..."
                                    value={customType}
                                    onChange={(e) => setCustomType(e.target.value)}
                                    onKeyDown={(e) => {
                                        if (e.key === "Enter" && !e.nativeEvent.isComposing) handleCustomContinue();
                                    }}
                                    autoFocus
                                />
                                {error && <p className="onb-error">{error}</p>}
                                <button type="button" className="onb-primary" onClick={handleCustomContinue}>
                                    Continue
                                </button>
                            </div>
                        )}
                    </div>
                )}

                {/* STEP 2 */}
                {step === 2 && (
                    <div className="onb-step" key="step2">
                        <h1 className="onb-title">What country are you in?</h1>
                        <p className="onb-sub">This helps us find your bank.</p>

                        <label className="onb-label" htmlFor="country">Country</label>
                        <select
                            id="country"
                            className="onb-select"
                            value={country}
                            onChange={(e) => handleCountryChange(e.target.value)}
                        >
                            <option value="">Choose your country</option>
                            {COUNTRIES.map((c) => (
                                <option key={c} value={c}>{c}</option>
                            ))}
                        </select>

                        {country && (
                            <div className="onb-bank-block">
                                <label className="onb-label" htmlFor="bank">What bank do you use?</label>

                                {banksLoading ? (
                                    /* Animated loading experience: a live status line, an
                                       indeterminate progress bar, and shimmering skeleton
                                       rows so the wait feels like real work is happening. */
                                    <div className="onb-bankloader" aria-live="polite" aria-busy="true">
                                        <div className="onb-bankloader-head">
                                            <span className="onb-bankloader-spinner" />
                                            <span className="onb-bankloader-text">{bankLoaderText}</span>
                                        </div>
                                        <div className="onb-bankloader-bar">
                                            <span className="onb-bankloader-bar-fill" />
                                        </div>
                                        <div className="onb-skeleton-list">
                                            {[0, 1, 2, 3].map((i) => (
                                                <div className="onb-skeleton-row" key={i}>
                                                    <span className="onb-skeleton-avatar" />
                                                    <span
                                                        className="onb-skeleton-line"
                                                        style={{ width: `${70 - i * 12}%` }}
                                                    />
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                ) : (
                                    <div className="onb-search-wrap">
                                        <input
                                            id="bank"
                                            className="onb-input"
                                            placeholder="Type your bank name..."
                                            value={bankQuery}
                                            onChange={(e) => {
                                                setBankQuery(e.target.value);
                                                setSelectedBank(null);
                                                setShowSuggestions(true);
                                            }}
                                            onFocus={() => setShowSuggestions(true)}
                                        />
                                        {showSuggestions && !selectedBank && filteredBanks.length > 0 && (
                                            <ul className="onb-suggestions">
                                                {filteredBanks.map((b) => (
                                                    <li key={b.name} onClick={() => handlePickBank(b)}>
                                                        {b.name}
                                                    </li>
                                                ))}
                                            </ul>
                                        )}
                                    </div>
                                )}

                                {error && <p className="onb-error">{error}</p>}

                                {selectedBank && (
                                    <button type="button" className="onb-primary" onClick={() => setStep(3)}>
                                        Continue
                                    </button>
                                )}
                            </div>
                        )}

                        <button type="button" className="onb-back" onClick={() => setStep(1)}>
                            Go back
                        </button>
                    </div>
                )}

                {/* STEP 3 */}
                {step === 3 && (
                    <div className="onb-step" key="step3">
                        <h1 className="onb-title">Connect your bank</h1>
                        <p className="onb-sub">One last step to see your money.</p>

                        <div className="onb-safe">
                            Loamy only reads your bank alert emails to track your balance and catch leaks.
                            We never read your personal messages, and your data stays 100% safe and encrypted.
                        </div>

                        <button type="button" className="onb-primary onb-connect" onClick={handleConnectGmail}>
                            <i className="fa-solid fa-key" aria-hidden="true" />
                            Connect My Gmail
                        </button>

                        <button type="button" className="onb-back" onClick={() => setStep(2)}>
                            Go back
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}
