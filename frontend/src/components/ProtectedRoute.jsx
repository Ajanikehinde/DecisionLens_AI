import React from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";

export default function ProtectedRoute({ children }) {
  const { user, ready } = useAuth();
  if (!ready) {
    return (
      <div
        data-testid="auth-loading"
        className="min-h-screen flex items-center justify-center text-slate-400"
      >
        <div className="font-mono text-xs tracking-widest uppercase">Loading DecisionLens…</div>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  return children;
}
