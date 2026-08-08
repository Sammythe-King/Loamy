import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from "react-router-dom";

// Pages
import Login from './Login.jsx'
import SignUp from './SignUp.jsx'
import Dashboard from './Dashboard.jsx'
import ChatPage from './ChatPage.jsx'
import GoalsPage from './GoalsPage.jsx'
import GmailConnectPage from './GmailConnectPage.jsx'
import InvoicesPage from './InvoicesPage.jsx'
import ReviewQueuePage from './ReviewQueuePage.jsx'

// Styles
import './index.css'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        {/* Auth Routes */}
        <Route path="/" element={<Login />} />
        <Route path="/signup" element={<SignUp />} />
        
        {/* Main App Routes */}
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/goals" element={<GoalsPage />} />
        <Route path="/gmail-connect" element={<GmailConnectPage />} />
        <Route path="/invoices" element={<InvoicesPage />} />
        <Route path="/review-queue" element={<ReviewQueuePage />} />
        
        {/* Aliases for convenience */}
        <Route path="/spending" element={<Dashboard />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)

import React from "react";
const ProgressBar = ({ value }) => {
  return (

    // <div>
    //     hi

    // </div>
   
    <div style={{
      width: "100%",
      height: "10px",
      background: "#D4DA7B",
      borderRadius: "20px",
      margin:"10px 0",
      overflow: "hidden"
    }}>
      <div style={{
        width: `${value}%`,
        height: "100%",
        background: "#768E52",
        transition: "width 0.3s linear",
      }} />

      
    </div>
  );
};

// Usage
{/* <ProgressBar value={40} /> */}
export default ProgressBar;