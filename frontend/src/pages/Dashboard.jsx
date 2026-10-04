import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import {
  TrendingUp,
  TrendingDown,
  ArrowUpRight,
  Lightbulb,
  ShieldAlert,
  AlertTriangle,
  Users,
  Target,
  Activity,
  CheckCircle2,
  Circle,
} from "lucide-react";
import api, { formatApiError } from "@/lib/api";
import { Link } from "react-router-dom";

const CATEGORY_LABEL = {
  business_strategy: "Strategy",
  hiring: "Hiring",
  investment: "Investment",
  operations: "Operations",
  marketing: "Marketing",
  technology: "Technology",
};

const RISK_COLORS = {
  low: "#10b981",
  medium: "#f59e0b",
  high: "#f97316",
  critical: "#ef4444",
};

const RISK_LABEL = {
  low: "Low Risk",
  medium: "Medium Risk",
  high: "High Risk",
  critical: "Critical Risk",
};

const ICONS = {
  "alert-triangle": AlertTriangle,
  "shield-alert": ShieldAlert,
  users: Users,
  target: Target,
  activity: Activity,
  lightbulb: Lightbulb,
};

function KpiCard({ label, value, suffix, delta, tone = "up", testid }) {
  const ToneIcon = tone === "up" ? TrendingUp : TrendingDown;
  return (
    <div className="card p-5" data-testid={testid}>
      <div className="flex items-start justify-between">
        <div className="label-eyebrow">{label}</div>
        <div
          className={`flex items-center gap-1 text-[11px] font-mono ${
            tone === "up" ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          <ToneIcon size={12} />
          {delta}%
        </div>
      </div>
      <div className="metric-num text-4xl mt-2">
        {value}
        {suffix && <span className="text-xl text-slate-500 ml-0.5">{suffix}</span>}
      </div>
      <div className="mt-3 h-1 rounded-full overflow-hidden bg-white/5">
        <div
          className="h-full bg-gradient-to-r from-violet-500 to-blue-500"
          style={{ width: `${Math.min(100, Math.abs(delta) * 6)}%` }}
        />
      </div>
    </div>
  );
}

function HealthRing({ score }) {
  const radius = 56;
  const circ = 2 * Math.PI * radius;
  const pct = Math.max(0, Math.min(100, score));
  const dash = (pct / 100) * circ;
  return (
    <div className="relative w-[140px] h-[140px] flex items-center justify-center">
      <svg width="140" height="140" className="-rotate-90">
        <defs>
          <linearGradient id="ringGrad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="#a78bfa" />
            <stop offset="100%" stopColor="#60a5fa" />
          </linearGradient>
        </defs>
        <circle cx="70" cy="70" r={radius} stroke="rgba(255,255,255,0.06)" strokeWidth="10" fill="none" />
        <circle
          cx="70"
          cy="70"
          r={radius}
          stroke="url(#ringGrad)"
          strokeWidth="10"
          fill="none"
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circ}`}
          style={{ filter: "drop-shadow(0 0 12px rgba(139,92,246,0.55))" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <div className="metric-num text-4xl leading-none" data-testid="health-score">
          {score}
        </div>
        <div className="font-mono text-[10px] tracking-widest uppercase text-slate-500 mt-1">
          / 100
        </div>
      </div>
    </div>
  );
}

function StatusPill({ status }) {
  const cls =
    {
      active: "pill pill-active",
      complete: "pill pill-complete",
      review: "pill pill-review",
      draft: "pill pill-draft",
    }[status] || "pill pill-draft";
  return <span className={cls}>{status}</span>;
}

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [recs, setRecs] = useState([]);
  const [error, setError] = useState(null);
  const [trendMode, setTrendMode] = useState("daily");

  const load = useCallback(async () => {
    setError(null);
    try {
      const [summary, recommendations] = await Promise.all([
        api.get("/dashboard/summary"),
        api.get("/recommendations"),
      ]);
      setData(summary.data);
      setRecs(recommendations.data);
    } catch (e) {
      setError(`Could not load your dashboard: ${formatApiError(e.response?.data?.detail)}`);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const trendSeries = useMemo(() => {
    if (!data) return [];
    return data.trends[trendMode].map((d) => ({ ...d, label: d.date }));
  }, [data, trendMode]);

  if (!data && !error) {
    return (
      <div className="space-y-6" data-testid="dashboard-loading">
        <div className="skeleton h-7 w-64" />
        <div className="grid grid-cols-4 gap-4">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="skeleton h-28" />
          ))}
        </div>
        <div className="skeleton h-72" />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="space-y-6" data-testid="dashboard-error">
        <div
          role="alert"
          className="card p-4 border-rose-500/30 bg-rose-500/5 flex items-start gap-3"
        >
          <AlertTriangle size={16} className="text-rose-400 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p className="text-sm text-rose-200">{error}</p>
            <button onClick={load} className="btn btn-secondary mt-2" data-testid="dashboard-retry">
              Try again
            </button>
          </div>
        </div>
      </div>
    );
  }

  const k = data.kpis;

  return (
    <div className="space-y-6" data-testid="dashboard-page">
      {/* Header */}
      <div className="flex items-end justify-between">
        <div>
          <div className="label-eyebrow">Decision Cockpit</div>
          <h1 className="font-sora text-3xl font-semibold mt-1">Dashboard</h1>
        </div>
        <Link to="/decisions/new" className="btn btn-primary" data-testid="cta-new-decision">
          + New Decision
        </Link>
      </div>

      {/* KPI Row */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <KpiCard label="Total Decisions" value={k.total.value} delta={k.total.delta} testid="kpi-total" />
        <KpiCard label="Active" value={k.active.value} delta={k.active.delta} testid="kpi-active" />
        <KpiCard
          label="Completed"
          value={k.completed.value}
          delta={k.completed.delta}
          testid="kpi-completed"
        />
        <KpiCard
          label="Avg Confidence"
          value={k.confidence.value}
          suffix="%"
          delta={k.confidence.delta}
          testid="kpi-confidence"
        />
      </div>

      {/* Signature + Trends */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        {/* Decision Health Score */}
        <div className="signature-card p-6 lg:col-span-1" data-testid="health-card">
          <div className="flex items-center justify-between mb-3">
            <div>
              <div className="label-eyebrow">Signature Metric</div>
              <div className="font-sora text-lg font-semibold mt-0.5">Decision Health</div>
            </div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-violet-300/80">
              Live
            </div>
          </div>
          <div className="flex flex-col items-center">
            <HealthRing score={data.health.score} />
            <div className="grid grid-cols-1 gap-2 w-full mt-5">
              {data.health.factors.map((f) => (
                <div
                  key={f.label}
                  className="flex items-center justify-between text-xs"
                  data-testid={`health-factor-${f.label.toLowerCase().replace(/ /g, "-")}`}
                >
                  <div className="flex items-center gap-2 text-slate-300">
                    {f.ok ? (
                      <CheckCircle2 size={13} className="text-emerald-400" />
                    ) : (
                      <Circle size={13} className="text-orange-400" />
                    )}
                    {f.label}
                  </div>
                  <div className="font-mono text-slate-400">{f.score}</div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Decision Trends */}
        <div className="card p-6 lg:col-span-3" data-testid="trends-card">
          <div className="flex items-center justify-between mb-4">
            <div>
              <div className="label-eyebrow">Activity</div>
              <div className="font-sora text-lg font-semibold mt-0.5">Decision Trends</div>
            </div>
            <div className="flex gap-1 bg-white/5 p-1 rounded-lg">
              {["daily", "weekly", "monthly"].map((m) => (
                <button
                  key={m}
                  onClick={() => setTrendMode(m)}
                  className={`tab ${trendMode === m ? "active" : ""}`}
                  data-testid={`trend-tab-${m}`}
                >
                  {m[0].toUpperCase() + m.slice(1)}
                </button>
              ))}
            </div>
          </div>
          <ResponsiveContainer width="100%" height={260}>
            <LineChart data={trendSeries}>
              <defs>
                <linearGradient id="lineGrad" x1="0" y1="0" x2="1" y2="0">
                  <stop offset="0%" stopColor="#8b5cf6" />
                  <stop offset="100%" stopColor="#3b82f6" />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(255,255,255,0.04)" vertical={false} />
              <XAxis
                dataKey="label"
                stroke="#475569"
                tick={{ fontSize: 10, fill: "#64748b", fontFamily: "JetBrains Mono" }}
                tickLine={false}
                axisLine={false}
                interval="preserveStartEnd"
              />
              <YAxis
                stroke="#475569"
                tick={{ fontSize: 10, fill: "#64748b", fontFamily: "JetBrains Mono" }}
                tickLine={false}
                axisLine={false}
                width={32}
              />
              <Tooltip
                contentStyle={{
                  background: "rgba(15,23,42,0.92)",
                  border: "1px solid rgba(139,92,246,0.3)",
                  borderRadius: 10,
                  fontFamily: "JetBrains Mono",
                  fontSize: 11,
                }}
                labelStyle={{ color: "#cbd5e1" }}
              />
              <Line
                type="monotone"
                dataKey="count"
                stroke="url(#lineGrad)"
                strokeWidth={2.5}
                dot={{ r: 3, fill: "#8b5cf6", strokeWidth: 0 }}
                activeDot={{ r: 5, fill: "#a78bfa" }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Categories + Risk */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-6" data-testid="categories-card">
          <div className="label-eyebrow">Breakdown</div>
          <div className="font-sora text-lg font-semibold mt-0.5 mb-4">Decision Categories</div>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={data.categories.map((c) => ({ ...c, name: CATEGORY_LABEL[c.category] }))}>
              <defs>
                <linearGradient id="barGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#fb923c" />
                  <stop offset="100%" stopColor="#f97316" />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="rgba(255,255,255,0.04)" vertical={false} />
              <XAxis
                dataKey="name"
                stroke="#475569"
                tick={{ fontSize: 10, fill: "#94a3b8" }}
                tickLine={false}
                axisLine={false}
              />
              <YAxis
                stroke="#475569"
                tick={{ fontSize: 10, fill: "#64748b", fontFamily: "JetBrains Mono" }}
                tickLine={false}
                axisLine={false}
                width={28}
              />
              <Tooltip
                cursor={{ fill: "rgba(139,92,246,0.06)" }}
                contentStyle={{
                  background: "rgba(15,23,42,0.92)",
                  border: "1px solid rgba(249,115,22,0.3)",
                  borderRadius: 10,
                  fontFamily: "JetBrains Mono",
                  fontSize: 11,
                }}
              />
              <Bar
                dataKey="count"
                fill="url(#barGrad)"
                radius={[6, 6, 0, 0]}
                maxBarSize={48}
              />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card p-6" data-testid="risk-card">
          <div className="label-eyebrow">Exposure</div>
          <div className="font-sora text-lg font-semibold mt-0.5 mb-4">Risk Distribution</div>
          <div className="grid grid-cols-2 gap-4 items-center">
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie
                  data={data.risk_distribution}
                  dataKey="count"
                  nameKey="risk"
                  innerRadius={55}
                  outerRadius={85}
                  paddingAngle={3}
                  stroke="none"
                >
                  {data.risk_distribution.map((r) => (
                    <Cell key={r.risk} fill={RISK_COLORS[r.risk]} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{
                    background: "rgba(15,23,42,0.92)",
                    border: "1px solid rgba(139,92,246,0.3)",
                    borderRadius: 10,
                    fontFamily: "JetBrains Mono",
                    fontSize: 11,
                  }}
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="space-y-2">
              {data.risk_distribution.map((r) => (
                <div
                  key={r.risk}
                  className="flex items-center justify-between text-sm"
                  data-testid={`risk-${r.risk}`}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className="w-2.5 h-2.5 rounded-full"
                      style={{
                        background: RISK_COLORS[r.risk],
                        boxShadow: `0 0 10px ${RISK_COLORS[r.risk]}`,
                      }}
                    />
                    <span className="text-slate-300">{RISK_LABEL[r.risk]}</span>
                  </div>
                  <span className="font-mono text-slate-400">{r.count}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Recent + Recommendations */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="card p-6 lg:col-span-2" data-testid="recent-card">
          <div className="flex items-center justify-between mb-4">
            <div>
              <div className="label-eyebrow">Latest</div>
              <div className="font-sora text-lg font-semibold mt-0.5">Recent Decisions</div>
            </div>
            <Link to="/decisions" className="text-xs text-violet-300 hover:text-violet-200 flex items-center gap-1">
              View all <ArrowUpRight size={12} />
            </Link>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className="label-eyebrow text-left pb-3 border-b border-white/5">Decision</th>
                <th className="label-eyebrow text-left pb-3 border-b border-white/5">Status</th>
                <th className="label-eyebrow text-right pb-3 border-b border-white/5">Confidence</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_decisions.map((d) => (
                <tr key={d.id} className="border-b border-white/5 hover:bg-white/[0.02] transition-colors">
                  <td className="py-3 text-slate-200">
                    <Link to={`/decisions/${d.id}`} className="hover:text-violet-300" data-testid={`recent-open-${d.id}`}>
                      {d.title}
                    </Link>
                  </td>
                  <td className="py-3">
                    <StatusPill status={d.status} />
                  </td>
                  <td className="py-3 text-right font-mono">
                    <span
                      className={
                        d.confidence >= 85
                          ? "text-emerald-400"
                          : d.confidence >= 70
                          ? "text-amber-400"
                          : "text-rose-400"
                      }
                    >
                      {d.confidence}%
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="space-y-3" data-testid="recommendations-card">
          <div className="label-eyebrow">AI Insights</div>
          <div className="font-sora text-lg font-semibold mt-0.5">Recommendations</div>
          {recs.map((r, i) => {
            const Icon = ICONS[r.icon] || Lightbulb;
            const tone =
              r.severity === "danger"
                ? "border-rose-500/30 bg-rose-500/5"
                : r.severity === "warning"
                ? "border-orange-500/30 bg-orange-500/5"
                : "border-violet-500/30 bg-violet-500/5";
            const iconTone =
              r.severity === "danger"
                ? "text-rose-400"
                : r.severity === "warning"
                ? "text-orange-400"
                : "text-violet-300";
            return (
              <div
                key={r.id}
                className={`card p-4 border ${tone}`}
                data-testid={`rec-${i}`}
              >
                <div className="flex items-start gap-3">
                  <div className={`w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center ${iconTone}`}>
                    <Icon size={15} />
                  </div>
                  <div className="flex-1">
                    <div className="text-sm font-semibold text-white mb-0.5">
                      #{i + 1} · {r.title}
                    </div>
                    <div className="text-xs text-slate-400 leading-relaxed">{r.body}</div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
