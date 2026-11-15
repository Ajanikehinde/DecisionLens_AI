import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Trash2, Filter, Plus, Search, ArrowUpRight, AlertTriangle } from "lucide-react";
import api, { formatApiError } from "@/lib/api";

const CATS = ["business_strategy", "hiring", "investment", "operations", "marketing", "technology"];
const STATUSES = ["draft", "active", "review", "complete"];
const RISKS = ["low", "medium", "high", "critical"];

function StatusPill({ status }) {
  const cls = `pill pill-${status}`;
  return <span className={cls}>{status}</span>;
}
function RiskPill({ risk }) {
  const cls = `pill pill-${risk}`;
  return <span className={cls}>{risk}</span>;
}

/**
 * Row click -> open the decision. Ignores clicks that started on a link or button so
 * the title link (and the row-level delete button) keep their own behaviour.
 */
function openRow(e, id, navigate) {
  if (e.target.closest("a, button")) return;
  navigate(`/decisions/${id}`);
}

export default function Decisions() {
  const navigate = useNavigate();
  const [items, setItems] = useState(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [risk, setRisk] = useState("");
  const [category, setCategory] = useState("");
  const [loadError, setLoadError] = useState(null);
  const [rowError, setRowError] = useState(null);
  const [deletingId, setDeletingId] = useState(null);

  // Search is a server-side concern only. It used to be sent to the API *and*
  // re-applied client-side, which double-narrowed the result set once another
  // filter changed. useCallback keeps the identity stable across renders so the
  // effect below fires exactly once per filter change.
  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const params = {};
      if (status) params.status = status;
      if (risk) params.risk = risk;
      if (category) params.category = category;
      if (q.trim()) params.q = q.trim();
      const r = await api.get("/decisions", { params });
      setItems(r.data);
    } catch (e) {
      setLoadError(formatApiError(e.response?.data?.detail));
    }
  }, [status, risk, category, q]);

  // Debounced so typing does not fire a request per keystroke.
  useEffect(() => {
    const t = setTimeout(() => {
      load();
    }, 250);
    return () => clearTimeout(t);
  }, [load]);

  async function onDelete(id) {
    if (deletingId === id) return;
    if (!window.confirm("Delete this decision? This cannot be undone.")) return;
    setDeletingId(id);
    setRowError(null);
    try {
      await api.delete(`/decisions/${id}`);
      await load();
    } catch (e) {
      setRowError(formatApiError(e.response?.data?.detail));
    } finally {
      setDeletingId(null);
    }
  }

  const filtered = useMemo(() => items || [], [items]);

  return (
    <div className="space-y-6" data-testid="decisions-page">
      <div className="flex items-end justify-between">
        <div>
          <div className="label-eyebrow">Records</div>
          <h1 className="font-sora text-3xl font-semibold mt-1">Decisions</h1>
        </div>
        <Link to="/decisions/new" className="btn btn-primary" data-testid="new-decision-cta">
          <Plus size={14} /> New Decision
        </Link>
      </div>

      <div className="card p-4 flex flex-wrap items-center gap-3" data-testid="decisions-filters">
        <div className="relative flex-1 min-w-[200px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <label htmlFor="decisions-search" className="sr-only">Search decisions by title</label>
          <input
            id="decisions-search"
            data-testid="decisions-search"
            className="input pl-9"
            placeholder="Search by title…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <Filter size={14} className="text-slate-500" />
        <label htmlFor="filter-status" className="sr-only">Filter by status</label>
        <select
          id="filter-status"
          data-testid="filter-status"
          className="input !w-auto !py-2"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="">All status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <label htmlFor="filter-risk" className="sr-only">Filter by risk</label>
        <select
          id="filter-risk"
          data-testid="filter-risk"
          className="input !w-auto !py-2"
          value={risk}
          onChange={(e) => setRisk(e.target.value)}
        >
          <option value="">All risk</option>
          {RISKS.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <label htmlFor="filter-category" className="sr-only">Filter by category</label>
        <select
          id="filter-category"
          data-testid="filter-category"
          className="input !w-auto !py-2"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        >
          <option value="">All categories</option>
          {CATS.map((c) => (
            <option key={c} value={c}>{c.replace("_", " ")}</option>
          ))}
        </select>
      </div>

      {loadError && (
        <div
          role="alert"
          data-testid="decisions-error"
          className="card p-4 border-rose-500/30 bg-rose-500/5 flex items-start gap-3"
        >
          <AlertTriangle size={16} className="text-rose-400 mt-0.5 shrink-0" />
          <div className="flex-1">
            <p className="text-sm text-rose-200">Could not load decisions: {loadError}</p>
            <button onClick={load} className="btn btn-secondary mt-2" data-testid="decisions-retry">
              Try again
            </button>
          </div>
        </div>
      )}

      {rowError && (
        <p role="alert" className="text-sm text-rose-400" data-testid="decisions-row-error">
          {rowError}
        </p>
      )}

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-white/5">
              <th className="label-eyebrow text-left px-5 py-3">Title</th>
              <th className="label-eyebrow text-left px-5 py-3">Category</th>
              <th className="label-eyebrow text-left px-5 py-3">Status</th>
              <th className="label-eyebrow text-left px-5 py-3">Risk</th>
              <th className="label-eyebrow text-right px-5 py-3">Confidence</th>
              <th className="label-eyebrow text-right px-5 py-3">Created</th>
              <th className="px-5 py-3"></th>
            </tr>
          </thead>
          <tbody>
            {!items && (
              <tr>
                <td colSpan={7} className="px-5 py-8 text-center text-slate-500">
                    {loadError ? "Could not load decisions." : "Loading…"}
                  </td>
              </tr>
            )}
            {items && filtered.length === 0 && (
              <tr>
                <td colSpan={7} className="px-5 py-8 text-center text-slate-500" data-testid="no-decisions">
                  No decisions yet.
                </td>
              </tr>
            )}
            {filtered.map((d) => (
              <tr
                key={d.id}
                onClick={(e) => openRow(e, d.id, navigate)}
                className="border-b border-white/5 hover:bg-white/[0.02] transition-colors cursor-pointer"
                data-testid={`decision-row-${d.id}`}
              >
                <td className="px-5 py-3 text-slate-200 max-w-xs truncate">
                  <Link
                    to={`/decisions/${d.id}`}
                    className="hover:text-violet-300 inline-flex items-center gap-1"
                    data-testid={`open-${d.id}`}
                  >
                    {d.title}
                    <ArrowUpRight size={12} className="opacity-0 group-hover:opacity-100" />
                  </Link>
                </td>
                <td className="px-5 py-3 text-slate-400 text-xs uppercase tracking-wider font-mono">
                  {d.category.replace("_", " ")}
                </td>
                <td className="px-5 py-3"><StatusPill status={d.status} /></td>
                <td className="px-5 py-3"><RiskPill risk={d.risk} /></td>
                <td className="px-5 py-3 text-right font-mono">
                  <span className={d.confidence >= 85 ? "text-emerald-400" : d.confidence >= 70 ? "text-amber-400" : "text-rose-400"}>
                    {d.confidence}%
                  </span>
                </td>
                <td className="px-5 py-3 text-right text-xs text-slate-500 font-mono">
                  {d.created_at?.slice(0, 10)}
                </td>
                <td className="px-5 py-3 text-right">
                  <button
                    onClick={() => onDelete(d.id)}
                    disabled={deletingId === d.id}
                    aria-label={`Delete decision: ${d.title}`}
                    className="btn btn-ghost !p-1.5 hover:text-rose-400 disabled:opacity-50"
                    data-testid={`delete-${d.id}`}
                  >
                    <Trash2 size={14} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>
    </div>
  );
}
