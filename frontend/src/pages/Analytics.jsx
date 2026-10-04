import React, { useCallback, useEffect, useState } from "react";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
  ResponsiveContainer,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from "recharts";
import { AlertTriangle } from "lucide-react";
import api, { formatApiError } from "@/lib/api";

const CATEGORY_LABEL = {
  business_strategy: "Strategy",
  hiring: "Hiring",
  investment: "Investment",
  operations: "Operations",
  marketing: "Marketing",
  technology: "Technology",
};

export default function Analytics() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await api.get("/dashboard/summary");
      setData(r.data);
    } catch (e) {
      setError(`Could not load analytics: ${formatApiError(e.response?.data?.detail)}`);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!data && !error) {
    return (
      <div className="space-y-6" data-testid="analytics-loading">
        <div className="skeleton h-8 w-48" />
        <div className="skeleton h-80" />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="space-y-6" data-testid="analytics-error">
        <div
          role="alert"
          className="card p-4 border-rose-500/30 bg-rose-500/5 flex items-start gap-3"
        >
          <AlertTriangle size={16} className="text-rose-400 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p className="text-sm text-rose-200">{error}</p>
            <button onClick={load} className="btn btn-secondary mt-2" data-testid="analytics-retry">
              Try again
            </button>
          </div>
        </div>
      </div>
    );
  }

  const radar = data.categories.map((c) => ({
    category: CATEGORY_LABEL[c.category],
    value: c.count,
  }));

  return (
    <div className="space-y-6" data-testid="analytics-page">
      <div>
        <div className="label-eyebrow">Deep Dive</div>
        <h1 className="font-sora text-3xl font-semibold mt-1">Analytics</h1>
      </div>

      <div className="card p-6" data-testid="analytics-area">
        <div className="font-sora text-lg font-semibold mb-4">Monthly Decision Volume</div>
        <ResponsiveContainer width="100%" height={280}>
          <AreaChart data={data.trends.monthly}>
            <defs>
              <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#8b5cf6" stopOpacity={0.5} />
                <stop offset="100%" stopColor="#8b5cf6" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(255,255,255,0.04)" vertical={false} />
            <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 10, fill: "#64748b" }} tickLine={false} axisLine={false} />
            <YAxis stroke="#475569" tick={{ fontSize: 10, fill: "#64748b" }} tickLine={false} axisLine={false} width={32} />
            <Tooltip
              contentStyle={{
                background: "rgba(15,23,42,0.92)",
                border: "1px solid rgba(139,92,246,0.3)",
                borderRadius: 10,
                fontFamily: "JetBrains Mono",
                fontSize: 11,
              }}
            />
            <Area type="monotone" dataKey="count" stroke="#a78bfa" strokeWidth={2} fill="url(#areaGrad)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-6" data-testid="analytics-radar">
          <div className="font-sora text-lg font-semibold mb-4">Category Coverage</div>
          <ResponsiveContainer width="100%" height={300}>
            <RadarChart data={radar}>
              <PolarGrid stroke="rgba(255,255,255,0.08)" />
              <PolarAngleAxis dataKey="category" tick={{ fill: "#94a3b8", fontSize: 11 }} />
              <PolarRadiusAxis tick={{ fill: "#64748b", fontSize: 10 }} stroke="rgba(255,255,255,0.06)" />
              <Radar dataKey="value" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.35} />
              <Tooltip
                contentStyle={{
                  background: "rgba(15,23,42,0.92)",
                  border: "1px solid rgba(59,130,246,0.3)",
                  borderRadius: 10,
                  fontSize: 11,
                }}
              />
            </RadarChart>
          </ResponsiveContainer>
        </div>

        <div className="card p-6" data-testid="analytics-bars">
          <div className="font-sora text-lg font-semibold mb-4">Weekly Cadence</div>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={data.trends.weekly}>
              <CartesianGrid stroke="rgba(255,255,255,0.04)" vertical={false} />
              <XAxis dataKey="date" stroke="#475569" tick={{ fontSize: 9, fill: "#64748b" }} tickLine={false} axisLine={false} />
              <YAxis stroke="#475569" tick={{ fontSize: 10, fill: "#64748b" }} tickLine={false} axisLine={false} width={28} />
              <Tooltip
                contentStyle={{
                  background: "rgba(15,23,42,0.92)",
                  border: "1px solid rgba(20,184,166,0.3)",
                  borderRadius: 10,
                  fontFamily: "JetBrains Mono",
                  fontSize: 11,
                }}
              />
              <Bar dataKey="count" fill="#14b8a6" radius={[4, 4, 0, 0]} maxBarSize={28} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
