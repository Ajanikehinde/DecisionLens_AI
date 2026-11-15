import React, { useCallback, useEffect, useState } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import api, { formatApiError } from "@/lib/api";
import { Check, Sparkles, Crown, Rocket, ArrowRight , AlertTriangle } from "lucide-react";

const TIER_ICONS = {
  starter: Rocket,
  pro: Sparkles,
  team: Crown,
};

const TIER_THEME = {
  starter: {
    border: "border-white/10",
    ring: "",
    badge: "text-slate-300 border-white/10 bg-white/5",
    glow: "",
  },
  pro: {
    border: "border-violet-500/40",
    ring: "ring-2 ring-violet-500/30",
    badge: "text-violet-200 border-violet-500/40 bg-violet-500/10",
    glow: "shadow-[0_0_60px_-10px_rgba(139,92,246,0.45)]",
  },
  team: {
    border: "border-blue-500/30",
    ring: "",
    badge: "text-blue-200 border-blue-500/30 bg-blue-500/10",
    glow: "",
  },
};

function PollingBanner({ status }) {
  return (
    <div
      data-testid="checkout-polling-banner"
      className="card p-4 border border-violet-500/30 bg-violet-500/5 flex items-center gap-3"
    >
      <span className="inline-flex gap-1">
        <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
        <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse [animation-delay:200ms]" />
        <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse [animation-delay:400ms]" />
      </span>
      <span className="text-sm text-violet-200">{status}</span>
    </div>
  );
}

export default function Pricing() {
  const [tiers, setTiers] = useState([]);
  const [loadError, setLoadError] = useState(null);
  const [me, setMe] = useState(null);
  const [loadingTier, setLoadingTier] = useState(null);
  const [polling, setPolling] = useState(null); // {status, kind:"pending|success|failed"}
  const [err, setErr] = useState("");
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const [t, m] = await Promise.all([
        api.get("/billing/tiers"),
        api.get("/billing/me"),
      ]);
      setTiers(t.data);
      setMe(m.data);
    } catch (e) {
      // Without this, a failure left `tiers` empty and the page rendered as a blank
      // area with no message at all.
      setLoadError(`Could not load pricing: ${formatApiError(e.response?.data?.detail)}`);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Handle return from Stripe
  useEffect(() => {
    const sessionId = params.get("session_id");
    const canceled = params.get("canceled");
    if (canceled) {
      setPolling({ status: "Payment canceled. No charge made.", kind: "failed" });
      params.delete("canceled");
      setParams(params, { replace: true });
      return;
    }
    if (!sessionId) return;
    setPolling({ status: "Confirming payment with Stripe…", kind: "pending" });
    let attempts = 0;
    const maxAttempts = 10;
    const poll = async () => {
      attempts += 1;
      try {
        const r = await api.get(`/billing/status/${sessionId}`);
        const ps = r.data.payment_status;
        if (ps === "paid") {
          setPolling({
            status: `Payment confirmed — ${r.data.tier?.toUpperCase()} tier activated! 🎉`,
            kind: "success",
          });
          refresh();
          params.delete("session_id");
          setParams(params, { replace: true });
          return;
        }
        if (r.data.status === "expired") {
          setPolling({ status: "Stripe session expired. Try again.", kind: "failed" });
          params.delete("session_id");
          setParams(params, { replace: true });
          return;
        }
        if (attempts < maxAttempts) {
          setTimeout(poll, 2000);
        } else {
          setPolling({
            status: "Still processing. Refresh in a minute to see your tier update.",
            kind: "failed",
          });
        }
      } catch (e) {
        setPolling({
          status: formatApiError(e.response?.data?.detail) || "Failed to verify payment.",
          kind: "failed",
        });
      }
    };
    poll();
    // eslint-disable-next-line
  }, []);

  async function subscribe(tierId) {
    if (tierId === "starter") return;
    setErr("");
    setLoadingTier(tierId);
    try {
      // No origin_url: the backend builds Stripe redirects from its own configuration.
      const r = await api.post("/billing/checkout", { tier: tierId });
      // redirect to Stripe-hosted checkout
      window.location.href = r.data.url;
    } catch (e) {
      setErr(formatApiError(e.response?.data?.detail) || e.message);
      setLoadingTier(null);
    }
  }

  return (
    <div className="space-y-8" data-testid="pricing-page">
      <div className="text-center max-w-2xl mx-auto">
        <div className="label-eyebrow">Pricing</div>
        <h1 className="font-sora text-4xl font-semibold mt-2 leading-tight">
          Pay for the decisions that matter.
        </h1>
        <p className="text-slate-400 text-sm mt-3 leading-relaxed">
          Start free. Upgrade when your team needs unlimited AI critique, deeper analytics,
          and shared decision portfolios. Cancel any time.
        </p>
        {me && me.tier !== "starter" && (
          <div
            data-testid="current-tier-banner"
            className="mt-4 inline-flex items-center gap-2 text-xs font-mono uppercase tracking-widest border border-emerald-500/40 bg-emerald-500/10 text-emerald-300 rounded-full px-3 py-1.5"
          >
            <Check size={12} /> You&apos;re on {me.tier_meta?.name}
            {me.current_period_end && (
              <span className="text-emerald-400/70">
                · renews {me.current_period_end.slice(0, 10)}
              </span>
            )}
          </div>
        )}
      </div>

      {polling && (
        <div className="max-w-2xl mx-auto">
          {polling.kind === "pending" && <PollingBanner status={polling.status} />}
          {polling.kind === "success" && (
            <div
              data-testid="checkout-success-banner"
              className="card p-4 border border-emerald-500/30 bg-emerald-500/5 text-emerald-200 text-sm"
            >
              {polling.status}
            </div>
          )}
          {polling.kind === "failed" && (
            <div
              data-testid="checkout-fail-banner"
              className="card p-4 border border-rose-500/30 bg-rose-500/5 text-rose-200 text-sm"
            >
              {polling.status}
            </div>
          )}
        </div>
      )}

      {err && (
        <div className="max-w-2xl mx-auto text-sm text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-lg px-3 py-2">
          {err}
        </div>
      )}

      {loadError && (
        <div
          role="alert"
          data-testid="pricing-error"
          className="max-w-2xl mx-auto card p-4 border-rose-500/30 bg-rose-500/5 flex items-start gap-3"
        >
          <AlertTriangle size={16} className="text-rose-400 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p className="text-sm text-rose-200">{loadError}</p>
            <button onClick={refresh} className="btn btn-secondary mt-2" data-testid="pricing-retry">
              Try again
            </button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 max-w-6xl mx-auto">
        {tiers.map((t) => {
          const Icon = TIER_ICONS[t.id] || Sparkles;
          const theme = TIER_THEME[t.id] || TIER_THEME.starter;
          const isCurrent = me?.tier === t.id;
          const isFree = t.price === 0;
          return (
            <div
              key={t.id}
              className={`card p-6 relative ${theme.border} ${theme.ring} ${theme.glow}`}
              data-testid={`tier-card-${t.id}`}
            >
              {t.id === "pro" && (
                <div className="absolute -top-3 left-1/2 -translate-x-1/2 px-3 py-1 rounded-full bg-violet-600 text-white text-[10px] font-mono uppercase tracking-widest shadow-[0_0_20px_rgba(139,92,246,0.5)]">
                  Most Popular
                </div>
              )}
              <div className="flex items-center gap-3 mb-4">
                <div
                  className={`w-10 h-10 rounded-lg border flex items-center justify-center ${theme.badge}`}
                >
                  <Icon size={16} />
                </div>
                <div>
                  <div className="font-sora text-lg font-semibold">{t.name}</div>
                  <div className="text-xs text-slate-500">{t.tagline}</div>
                </div>
              </div>

              <div className="flex items-baseline gap-1">
                <span className="metric-num text-4xl">
                  ${t.price.toFixed(0)}
                </span>
                {!isFree && <span className="text-slate-500 text-sm">/ month</span>}
              </div>
              <div className="text-xs text-slate-500 font-mono mt-1">{t.billed}</div>

              <div className="my-5 h-px bg-white/5" />

              <ul className="space-y-2.5 mb-6">
                {t.features.map((f) => (
                  <li key={f} className="flex items-start gap-2 text-sm text-slate-300">
                    <Check size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                    {f}
                  </li>
                ))}
              </ul>

              {isCurrent ? (
                <button
                  disabled
                  className="btn btn-secondary w-full justify-center opacity-60 cursor-not-allowed"
                  data-testid={`tier-current-${t.id}`}
                >
                  <Check size={14} /> Your current plan
                </button>
              ) : isFree ? (
                <button
                  onClick={() => navigate("/")}
                  className="btn btn-secondary w-full justify-center"
                  data-testid={`tier-btn-${t.id}`}
                >
                  Continue free
                </button>
              ) : (
                <button
                  onClick={() => subscribe(t.id)}
                  disabled={loadingTier === t.id}
                  className={`btn w-full justify-center ${
                    t.id === "pro" ? "btn-primary" : "btn-secondary"
                  } disabled:opacity-60`}
                  data-testid={`tier-btn-${t.id}`}
                >
                  {loadingTier === t.id ? "Redirecting…" : `Upgrade to ${t.name}`}
                  <ArrowRight size={14} />
                </button>
              )}
            </div>
          );
        })}
      </div>

      <div className="max-w-3xl mx-auto text-center text-[11px] font-mono text-slate-600 leading-relaxed">
        Payments processed securely by Stripe · Test mode · Use card{" "}
        <span className="text-slate-400">4242 4242 4242 4242</span> any future date · any CVC
      </div>
    </div>
  );
}
