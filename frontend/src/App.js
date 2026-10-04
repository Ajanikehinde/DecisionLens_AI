import React from "react";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "@/contexts/AuthContext";
import ProtectedRoute from "@/components/ProtectedRoute";
import Layout from "@/components/Layout";
import Login from "@/pages/Login";
import Register from "@/pages/Register";
import Dashboard from "@/pages/Dashboard";
import Decisions from "@/pages/Decisions";
import NewDecision from "@/pages/NewDecision";
import AIChat from "@/pages/AIChat";
import Analytics from "@/pages/Analytics";
import Sources from "@/pages/Sources";
import Settings from "@/pages/Settings";
import About from "@/pages/About";
import DecisionDetail from "@/pages/DecisionDetail";
import Pricing from "@/pages/Pricing";

function App() {
  return (
    <div className="App">
      <BrowserRouter>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route
              path="/"
              element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }
            >
              <Route index element={<Dashboard />} />
              <Route path="decisions" element={<Decisions />} />
              <Route path="decisions/new" element={<NewDecision />} />
              <Route path="decisions/:id" element={<DecisionDetail />} />
              <Route path="chat" element={<AIChat />} />
              <Route path="analytics" element={<Analytics />} />
              <Route path="sources" element={<Sources />} />
              <Route path="pricing" element={<Pricing />} />
              <Route path="settings" element={<Settings />} />
              <Route path="about" element={<About />} />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </AuthProvider>
      </BrowserRouter>
    </div>
  );
}

export default App;
