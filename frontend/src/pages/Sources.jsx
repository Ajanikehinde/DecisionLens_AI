import React, { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import { Database, CheckCircle2, AlertCircle, Loader2, Plug, PlugZap , AlertTriangle } from "lucide-react";

const STATUS_STYLES = {
  connected: { cls: "text-emerald-400 border-emerald-500/30 bg-emerald-500/5", icon: CheckCircle2 },
  syncing: { cls: "text-amber-400 border-amber-500/30 bg-amber-500/5", icon: Loader2 },
  disconnected: { cls: "text-slate-400 border-white/10 bg-white/5", icon: AlertCircle },
};

const OAUTH_PROVIDERS = new Set(["salesforce", "hubspot"]);

export default function Sources() {
  const [items, setItems] = useState(null);
  const [connections, setConnections] = useState({});
  const [busy, setBusy] = useState(null);
  const [banner, setBanner] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [params, setParams] = useSearchParams();

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [s, c] = await Promise.all([api.get("/sources"), api.get("/connections")]);
      setItems(s.data);
      setConnections(c.data);
    } catch (e) {
      setLoadError(`Could not load sources: ${formatApiError(e.response?.data?.detail)}`);
    }
  }, []);

  // Data fetch: intentionally independent of `params`. The OAuth callback returns via a
  // full page navigation, so this mount effect already runs with the query present;
  // adding `params` here would only trigger a duplicate fetch.
  useEffect(() => {
    load();
  }, [load]);

  // Consume the OAuth result from the URL, separately from fetching data.
  useEffect(() => {
    const connected = params.get("connected");
    const mock = params.get("mock");
    const error = params.get("error");
    if (connected) {
      setBanner({
        kind: "success",
        text: mock
          ? `Connected ${connected} (MOCK — no OAuth credentials configured)`
          : `Successfully connected ${connected}.`,
      });
      params.delete("connected");
      params.delete("mock");
      setParams(params, { replace: true });
    } else if (error) {
      setBanner({ kind: "error", text: `OAuth error: ${error}` });
      params.delete("error");
      setParams(params, { replace: true });
    }
    // This handler self-limits by deleting every key it reads, so it is safe to leave
    // `params` out of the dependency list: re-running it would find nothing to do.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connect(provider) {
    if (!OAUTH_PROVIDERS.has(provider)) return;
    setBusy(provider);
    try {
      const r = await api.get(`/oauth/${provider}/start`);
      if (r.data.mock) {
        // mock — already connected server-side, just reload
        setBanner({
          kind: "success",
          text: `Connected ${provider} (MOCK mode — add ${provider.toUpperCase()}_CLIENT_ID + SECRET to backend .env to enable real OAuth).`,
        });
        load();
      } else {
        window.location.href = r.data.redirect_url;
      }
    } finally {
      setBusy(null);
    }
  }

  async function disconnect(provider) {
    setBusy(provider);
    try {
      await api.post(`/oauth/${provider}/disconnect`);
      load();
    } finally {
      setBusy(null);
    }
  }

  // merge static catalog with live connection state for oauth providers
  const merged = (items || []).map((s) => {
    const provider = s.id; // salesforce/hubspot/...
    if (OAUTH_PROVIDERS.has(provider)) {
      const conn = connections[provider];
      if (conn?.status === "connected") {
        return { ...s, status: "connected", mock: conn.mock };
      }
      if (conn?.status === "disconnected") {
        return { ...s, status: "disconnected" };
      }
      return { ...s, status: "disconnected" };
    }
    return s;
  });

  return (
    <div className="space-y-6" data-testid="sources-page">
      <div className="flex items-end justify-between">
        <div>
          <div className="label-eyebrow">Data In</div>
          <h1 className="font-sora text-3xl font-semibold mt-1">Sources</h1>
          <p className="text-slate-400 text-sm mt-1">
            Connect your systems so DecisionLens can score with real data.
          </p>
        </div>
      </div>

      {banner && (
        <div
          data-testid="sources-banner"
          className={`card p-3 text-sm border ${
            banner.kind === "success"
              ? "border-emerald-500/30 bg-emerald-500/5 text-emerald-300"
              : "border-rose-500/30 bg-rose-500/5 text-rose-300"
          }`}
        >
          {banner.text}
        </div>
      )}

      {loadError && (
        <div
          role="alert"
          data-testid="sources-error"
          className="card p-4 border-rose-500/30 bg-rose-500/5 flex items-start gap-3"
        >
          <AlertTriangle size={16} className="text-rose-400 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p className="text-sm text-rose-200">{loadError}</p>
            <button onClick={load} className="btn btn-secondary mt-2" data-testid="sources-retry">
              Try again
            </button>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {!items &&
          [1, 2, 3, 4, 5, 6].map((i) => <div key={i} className="skeleton h-44" />)}
        {merged.map((s) => {
          const conf = STATUS_STYLES[s.status] || STATUS_STYLES.disconnected;
          const Icon = conf.icon;
          const isOAuth = OAUTH_PROVIDERS.has(s.id);
          return (
            <div key={s.id} className="card p-5" data-testid={`source-${s.id}`}>
              <div className="flex items-start justify-between">
                <div className="w-10 h-10 rounded-lg bg-violet-600/10 border border-violet-500/20 flex items-center justify-center">
                  <Database size={16} className="text-violet-300" />
                </div>
                <div
                  className={`flex items-center gap-1 text-[10px] font-mono uppercase tracking-widest border rounded-full px-2 py-1 ${conf.cls}`}
                >
                  <Icon size={11} className={s.status === "syncing" ? "animate-spin" : ""} />
                  {s.status}
                  {s.mock && <span className="text-amber-400 ml-1">· MOCK</span>}
                </div>
              </div>
              <div className="mt-4 font-sora text-base font-semibold">{s.name}</div>
              <div className="text-xs text-slate-500 mt-0.5">{s.category}</div>
              <div className="grid grid-cols-2 gap-3 mt-4 pt-4 border-t border-white/5">
                <div>
                  <div className="label-eyebrow">Records</div>
                  <div className="metric-num text-base mt-0.5">
                    {(s.records || 0).toLocaleString()}
                  </div>
                </div>
                <div>
                  <div className="label-eyebrow">Last Sync</div>
                  <div className="text-xs font-mono text-slate-300 mt-1">
                    {s.last_sync ? new Date(s.last_sync).toLocaleTimeString() : "—"}
                  </div>
                </div>
              </div>
              {isOAuth && (
                <div className="mt-4 pt-4 border-t border-white/5">
                  {s.status === "connected" ? (
                    <button
                      onClick={() => disconnect(s.id)}
                      disabled={busy === s.id}
                      className="btn btn-secondary w-full justify-center disabled:opacity-60"
                      data-testid={`disconnect-${s.id}`}
                    >
                      <Plug size={14} />
                      {busy === s.id ? "Disconnecting…" : "Disconnect"}
                    </button>
                  ) : (
                    <button
                      onClick={() => connect(s.id)}
                      disabled={busy === s.id}
                      className="btn btn-primary w-full justify-center disabled:opacity-60"
                      data-testid={`connect-${s.id}`}
                    >
                      <PlugZap size={14} />
                      {busy === s.id ? "Connecting…" : `Connect ${s.name.split(" ")[0]}`}
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="text-[11px] font-mono text-slate-600 leading-relaxed">
        OAuth note: Salesforce + HubSpot use real OAuth 2.0. To enable production connections, add
        <span className="text-slate-400"> SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET, HUBSPOT_CLIENT_ID,
        HUBSPOT_CLIENT_SECRET </span>
        to backend/.env and configure the matching redirect URI in each provider&apos;s developer console.
      </div>
    </div>
  );
}
