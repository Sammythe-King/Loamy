import { Routes, Route, Navigate } from "react-router-dom";

// Pages
import Login from "./Login.jsx";
import SignUp from "./SignUp.jsx";
import Dashboard from "./Dashboard.jsx";
import ChatPage from "./ChatPage.jsx";
import GmailConnectPage from "./GmailConnectPage.jsx";
import InvoicesPage from "./InvoicesPage.jsx";
import ReviewQueuePage from "./ReviewQueuePage.jsx";
import OnboardingWizard from "./OnboardingWizard.jsx";

// Global activity-driven sync. Wrapping the whole app (rather than a single
// page) means ANY interaction anywhere can trigger an automatic bank sync.
import { useGlobalActivitySync } from "./hooks/useGlobalActivitySync";

export default function App() {
  // The backend enforces the 5-minute cooldown, so it's safe to keep this on
  // app-wide; the hook just debounces and pings.
  useGlobalActivitySync({ enabled: true, debounceMs: 2000 });

  return (
    <Routes>
      {/* Auth Routes */}
      <Route path="/" element={<Login />} />
      <Route path="/onboarding" element={<OnboardingWizard />} />
      <Route path="/signup" element={<SignUp />} />

      {/* Main App Routes */}
      <Route path="/dashboard" element={<Dashboard />} />
      <Route path="/chat" element={<ChatPage />} />
      {/* Savings & Goals is hidden from the UI for now; send any deep links
          or bookmarks straight to the dashboard instead of a dead page. */}
      <Route path="/goals" element={<Navigate to="/dashboard" replace />} />
      <Route path="/gmail-connect" element={<GmailConnectPage />} />
      <Route path="/invoices" element={<InvoicesPage />} />
      <Route path="/review-queue" element={<ReviewQueuePage />} />

      {/* Aliases for convenience */}
      <Route path="/spending" element={<Dashboard />} />
    </Routes>
  );
}
