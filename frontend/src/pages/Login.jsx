import React, { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { ArrowRight } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import Logo from "@/components/Logo";

// Only emails are shown to the user. Passwords are never bundled into the client,
// so this form cannot leak a credential the way a hardcoded default would.
const DEMO_EMAIL = process.env.REACT_APP_DEMO_EMAIL || "demo@decisionlens.ai";
const ADMIN_EMAIL = process.env.REACT_APP_ADMIN_EMAIL || "admin@decisionlens.ai";
const showDemoHint = process.env.REACT_APP_SHOW_DEMO_HINT === "true";

export default function Login() {
  const navigate = useNavigate();
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    const r = await login(email, password);
    setLoading(false);
    if (r.ok) navigate("/");
    else setError(r.error);
  }

  return (
    <div className="min-h-screen grid lg:grid-cols-2" data-testid="login-page">
      {/* Left visual */}
      <div className="hidden lg:flex flex-col justify-between p-12 relative overflow-hidden bg-grid">
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            background:
              "radial-gradient(600px 400px at 20% 20%, rgba(139,92,246,0.18), transparent 60%), radial-gradient(500px 400px at 80% 70%, rgba(59,130,246,0.12), transparent 60%)",
          }}
        />
        <div className="relative">
          <div className="flex items-center gap-2">
            <Logo variant="mark" className="w-10 h-10 shrink-0" />
            <div className="font-sora text-lg font-semibold">DecisionLens AI</div>
          </div>
        </div>

        <div className="relative max-w-md">
          <div className="label-eyebrow mb-3">Signature Metric</div>
          <h1 className="font-sora text-4xl font-semibold leading-tight mb-4">
            Make decisions you can defend.
          </h1>
          <p className="text-slate-400 mb-8">
            A decision intelligence cockpit — health scoring, risk distribution, AI critique. Every
            call, on the record.
          </p>
          <div className="signature-card p-6">
            <div className="label-eyebrow mb-1">Decision Health Score</div>
            <div className="metric-num text-6xl">
              87<span className="text-3xl text-slate-500">/100</span>
            </div>
            <div className="grid grid-cols-2 gap-2 mt-4">
              {["Data Quality", "Stakeholder Alignment", "Risk Assessment", "AI Confidence"].map(
                (f) => (
                  <div key={f} className="flex items-center gap-2 text-xs text-slate-300">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,0.6)]" />
                    {f}
                  </div>
                )
              )}
            </div>
          </div>
        </div>

        <div className="relative font-mono text-[11px] tracking-widest uppercase text-slate-600">
          © 2026 · DecisionLens AI
        </div>
      </div>

      {/* Form */}
      <div className="flex items-center justify-center p-8">
        <form onSubmit={onSubmit} className="w-full max-w-sm" data-testid="login-form">
          <div className="label-eyebrow mb-2">Sign In</div>
          <h2 className="font-sora text-3xl font-semibold mb-1">Welcome back.</h2>
          <p className="text-slate-400 text-sm mb-8">
            Use the demo account or your own.
          </p>

          <label htmlFor="login-email-2" className="text-xs text-slate-400 mb-1 block">Email</label>
          <input id="login-email-2"
            data-testid="login-email"
            className="input mb-4"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />

          <label htmlFor="login-password-2" className="text-xs text-slate-400 mb-1 block">Password</label>
          <input id="login-password-2"
            data-testid="login-password"
            className="input mb-4"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />

          {error && (
            <div
              data-testid="login-error"
              className="text-sm text-rose-400 mb-3 bg-rose-500/10 border border-rose-500/20 rounded-lg px-3 py-2"
            >
              {error}
            </div>
          )}

          <button
            data-testid="login-submit"
            disabled={loading}
            className="btn btn-primary w-full justify-center mt-2 disabled:opacity-60"
            type="submit"
          >
            {loading ? "Signing in…" : "Sign In"} <ArrowRight size={14} />
          </button>

          <div className="text-xs text-slate-500 mt-5">
            New here?{" "}
            <Link to="/register" className="text-violet-300 hover:text-violet-200" data-testid="goto-register">
              Create an account
            </Link>
          </div>

          {showDemoHint ? (
            <div
              className="mt-8 text-[11px] font-mono text-slate-600 leading-relaxed"
              data-testid="login-demo-hint"
            >
              Demo: {DEMO_EMAIL} · see DEMO_PASSWORD in backend/.env
              <br />
              Admin: {ADMIN_EMAIL} · see ADMIN_PASSWORD in backend/.env
            </div>
          ) : (
            <div className="mt-8 text-[11px] font-mono text-slate-600 leading-relaxed">
              Seeded demo and admin accounts are created from ADMIN_EMAIL / ADMIN_PASSWORD
              and DEMO_EMAIL / DEMO_PASSWORD in backend/.env.
            </div>
          )}
        </form>
      </div>
    </div>
  );
}
