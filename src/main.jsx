import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import Dashboard from './Dashboard.jsx'
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import Login from './Login.jsx'
import LoamyHomePage from './LoamyHomePage.jsx';
import './index.css'
import SignUp from './SignUp'
import ProgressBar from './ProgressBar.jsx';
// import App from './App.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <LoamyHomePage/>
    {/* <ProgressBar value={40} /> */}
     {/* <BrowserRouter>
     <Routes>
         <Route path="/" element={<Login />} />
         <Route path="/signup" element={<SignUp />} />
         <Route path="/dashboard" element={<Dashboard />} />
       </Routes>
     </BrowserRouter> */}
  </StrictMode>,
)
