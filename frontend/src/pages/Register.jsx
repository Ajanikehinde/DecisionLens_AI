import React, { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Brain, ArrowRight } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

export default function Register() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    const r = await register(email, password, name);
    setLoading(false);
    if (r.ok) navigate("/");
    else setError(r.error);
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6" data-testid="register-page">
      <form onSubmit={onSubmit} className="w-full max-w-md card p-8" data-testid="register-form">
        <div className="flex items-center gap-2 mb-6">
          <div className="w-9 h-9 rounded-lg bg-violet-600/20 border border-violet-500/30 flex items-center justify-center glow-violet">
            <Brain size={18} className="text-violet-300" />
          </div>
          <div className="font-sora text-lg font-semibold">DecisionLens AI</div>
        </div>

        <div className="label-eyebrow mb-2">Create Account</div>
        <h2 className="font-sora text-2xl font-semibold mb-6">Start tracking better decisions.</h2>

        <label htmlFor="register-full-name" className="text-xs text-slate-400 mb-1 block">Full name</label>
        <input id="register-full-name"
          data-testid="reg-name"
          className="input mb-4"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />

        <label htmlFor="register-email" className="text-xs text-slate-400 mb-1 block">Email</label>
        <input id="register-email"
          data-testid="reg-email"
          className="input mb-4"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />

        <label htmlFor="register-password" className="text-xs text-slate-400 mb-1 block">Password</label>
        <input id="register-password"
          data-testid="reg-password"
          className="input mb-4"
          type="password"
          minLength={6}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />

        {error && (
          <div
            data-testid="reg-error"
            className="text-sm text-rose-400 mb-3 bg-rose-500/10 border border-rose-500/20 rounded-lg px-3 py-2"
          >
            {error}
          </div>
        )}

        <button
          data-testid="reg-submit"
          disabled={loading}
          className="btn btn-primary w-full justify-center mt-2 disabled:opacity-60"
        >
          {loading ? "Creating…" : "Create account"} <ArrowRight size={14} />
        </button>

        <div className="text-xs text-slate-500 mt-5 text-center">
          Already have an account?{" "}
          <Link to="/login" className="text-violet-300 hover:text-violet-200" data-testid="goto-login">
            Sign in
          </Link>
        </div>
      </form>
    </div>
  );
}
