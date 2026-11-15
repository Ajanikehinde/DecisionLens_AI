import React, { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, Link } from "react-router-dom";
import {
  Save,
  Trash2,
  Upload,
  FileText,
  MessageSquare,
  Clock,
  ArrowLeft,
  Paperclip,
  X,
  AlertTriangle,
  Sparkles,
  RefreshCw,
} from "lucide-react";
import api, { formatApiError, limitInfo } from "@/lib/api";
import Markdown from "@/components/Markdown";

const CATS = [
  ["business_strategy", "Business Strategy"],
  ["hiring", "Hiring"],
  ["investment", "Investment"],
  ["operations", "Operations"],
  ["marketing", "Marketing"],
  ["technology", "Technology"],
];
const STATUSES = [
  ["draft", "Draft"],
  ["active", "Active"],
  ["review", "In Review"],
  ["complete", "Complete"],
];
const RISKS = [
  ["low", "Low"],
  ["medium", "Medium"],
  ["high", "High"],
  ["critical", "Critical"],
];

function StatusPill({ status }) {
  return <span className={`pill pill-${status}`}>{status}</span>;
}
function RiskPill({ risk }) {
  return <span className={`pill pill-${risk}`}>{risk}</span>;
}

function fmtBytes(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}

const LIST_BLOCKS = [
  ["pros", "Pros", "text-emerald-300", "border-emerald-500/30 bg-emerald-500/5"],
  ["cons", "Cons", "text-rose-300", "border-rose-500/30 bg-rose-500/5"],
  ["risks", "Risks", "text-orange-300", "border-orange-500/30 bg-orange-500/5"],
  ["next_steps", "Next steps", "text-violet-300", "border-violet-500/30 bg-violet-500/5"],
];

function AnalysisPanel({ analysis, error, onRetry }) {
  if (error) {
    return (
      <div
        role="alert"
        data-testid="analysis-error"
        className="mt-4 card p-4 border border-rose-500/30 bg-rose-500/5"
      >
        <p className="text-sm text-rose-200 flex items-center gap-2">
          <AlertTriangle size={14} /> {error}
        </p>
        <button onClick={onRetry} className="btn btn-secondary mt-3" data-testid="analysis-retry">
          Try again
        </button>
      </div>
    );
  }

  if (!analysis) return null;

  return (
    <section className="mt-5 pt-4 border-t border-white/5" data-testid="analysis-panel">
      <div className="label-eyebrow mb-3 flex items-center gap-2">
        <Sparkles size={13} /> AI Analysis
      </div>

      {analysis.executive_summary && (
        <div className="mb-4">
          <div className="font-sora text-sm font-semibold text-white mb-1">Executive summary</div>
          <Markdown className="text-slate-300">{analysis.executive_summary}</Markdown>
        </div>
      )}

      {analysis.recommendation && (
        <div
          className="mb-4 p-3 rounded-lg border border-violet-500/30 bg-violet-500/10"
          data-testid="analysis-recommendation"
        >
          <div className="font-sora text-sm font-semibold text-violet-200 mb-1">Recommendation</div>
          <Markdown className="text-slate-200">{analysis.recommendation}</Markdown>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {LIST_BLOCKS.map(([key, label, textCls, boxCls]) => {
          const items = analysis[key];
          if (!Array.isArray(items) || items.length === 0) return null;
          return (
            <div key={key} className={`p-3 rounded-lg border ${boxCls}`} data-testid={`analysis-${key}`}>
              <div className={`font-sora text-sm font-semibold ${textCls} mb-1.5`}>{label}</div>
              <ul className="space-y-1 text-[13px] text-slate-300 list-disc pl-4 marker:text-slate-500">
                {items.map((it, i) => (
                  <li key={i}>{it}</li>
                ))}
              </ul>
            </div>
          );
        })}
      </div>
    </section>
  );
}

export default function DecisionDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [decision, setDecision] = useState(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [uploadLimit, setUploadLimit] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [analysing, setAnalysing] = useState(false);
  const [analysisErr, setAnalysisErr] = useState(null);

  const [comments, setComments] = useState([]);
  const [activity, setActivity] = useState([]);
  const [attachments, setAttachments] = useState([]);
  const [newComment, setNewComment] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef(null);

  const load = useCallback(async () => {
    setLoadError("");
    try {
      const [d, c, a, att] = await Promise.all([
        api.get(`/decisions/${id}`),
        api.get(`/decisions/${id}/comments`),
        api.get(`/decisions/${id}/activity`),
        api.get(`/decisions/${id}/attachments`),
      ]);
      setDecision(d.data);
      setForm({ ...d.data });
      setComments(c.data);
      setActivity(a.data);
      setAttachments(att.data);
    } catch (e) {
      const msg = formatApiError(e.response?.data?.detail) || e.message;
      setLoadError(msg);
      setErr(msg);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function saveEdit() {
    setSaving(true);
    setErr("");
    try {
      const payload = { ...form, confidence: Number(form.confidence) };
      delete payload.id;
      delete payload.user_id;
      delete payload.created_at;
      delete payload.updated_at;
      const r = await api.patch(`/decisions/${id}`, payload);
      setDecision(r.data);
      setEditing(false);
      load();
    } catch (e) {
      setErr(formatApiError(e.response?.data?.detail) || e.message);
    } finally {
      setSaving(false);
    }
  }

  async function onDelete() {
    if (!window.confirm("Delete this decision permanently?")) return;
    await api.delete(`/decisions/${id}`);
    navigate("/decisions");
  }

  async function addComment(e) {
    e.preventDefault();
    if (!newComment.trim()) return;
    await api.post(`/decisions/${id}/comments`, { body: newComment });
    setNewComment("");
    load();
  }

  async function uploadFile(file) {
    if (!file) return;
    setUploading(true);
    setErr("");
    setUploadLimit(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      await api.post(`/decisions/${id}/attachments`, fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      load();
    } catch (e) {
      setUploadLimit(limitInfo(e));
      setErr(formatApiError(e.response?.data?.detail) || e.message);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function deleteAttachment(attId) {
    if (!window.confirm("Delete this attachment?")) return;
    await api.delete(`/decisions/${id}/attachments/${attId}`);
    load();
  }

  async function runAnalysis() {
    if (analysing) return;
    setAnalysing(true);
    setAnalysisErr(null);
    try {
      const r = await api.post(`/decisions/${id}/analyse`, {});
      setDecision((d) => ({ ...d, ai_analysis: r.data.ai_analysis }));
    } catch (e) {
      setAnalysisErr(formatApiError(e.response?.data?.detail));
    } finally {
      setAnalysing(false);
    }
  }

  if (!decision || !form) {
    if (loadError) {
      return (
        <div className="space-y-4" data-testid="decision-detail-error">
          <div
            role="alert"
            className="card p-4 border-rose-500/30 bg-rose-500/5 flex items-start gap-3"
          >
            <AlertTriangle size={16} className="text-rose-400 mt-0.5 shrink-0" />
            <div className="flex-1">
              <p className="text-sm text-rose-200">
                Could not load this decision: {loadError}
              </p>
              <button
                onClick={load}
                className="btn btn-secondary mt-2"
                data-testid="decision-detail-retry"
              >
                Try again
              </button>
            </div>
          </div>
        </div>
      );
    }
    return (
      <div className="space-y-4" data-testid="decision-detail-loading">
        <div className="skeleton h-8 w-64" />
        <div className="skeleton h-40" />
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-6xl" data-testid="decision-detail-page">
      <div className="flex items-center justify-between">
        <Link to="/decisions" className="btn btn-ghost text-xs" data-testid="back-to-decisions">
          <ArrowLeft size={14} /> Decisions
        </Link>
        <div className="flex gap-2">
          {!editing && (
            <button
              className="btn btn-secondary"
              onClick={() => setEditing(true)}
              data-testid="edit-decision-btn"
            >
              Edit
            </button>
          )}
          <button
            className="btn btn-secondary disabled:opacity-60"
            onClick={runAnalysis}
            disabled={analysing}
            data-testid="analyse-btn"
          >
            {analysing ? <RefreshCw size={14} className="animate-spin" /> : <Sparkles size={14} />}
            {analysing ? "Analysing…" : decision.ai_analysis ? "Re-analyse" : "Analyse with AI"}
          </button>
          <button
            className="btn btn-ghost hover:text-rose-400"
            onClick={onDelete}
            data-testid="delete-decision-btn"
          >
            <Trash2 size={14} /> Delete
          </button>
        </div>
      </div>

      {/* Header / Edit */}
      <div className="card p-6">
        {!editing ? (
          <div>
            <div className="flex items-start justify-between gap-4 mb-3">
              <div>
                <div className="label-eyebrow">{decision.category.replace("_", " ")}</div>
                <h1 className="font-sora text-3xl font-semibold mt-1" data-testid="decision-title">
                  {decision.title}
                </h1>
              </div>
              <div className="flex flex-col items-end gap-2">
                <StatusPill status={decision.status} />
                <RiskPill risk={decision.risk} />
              </div>
            </div>
            {decision.description ? (
              <div className="mt-3 text-slate-300 text-sm" data-testid="decision-description">
                <Markdown className="text-slate-300">{decision.description}</Markdown>
              </div>
            ) : (
              <p className="text-slate-300 text-sm leading-relaxed mt-3 italic text-slate-500">
                No description.
              </p>
            )}
            <AnalysisPanel
              analysis={decision.ai_analysis}
              error={analysisErr}
              onRetry={runAnalysis}
            />

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6 pt-4 border-t border-white/5">
              <div>
                <div className="label-eyebrow">Confidence</div>
                <div className="metric-num text-2xl mt-1">{decision.confidence}%</div>
              </div>
              <div>
                <div className="label-eyebrow">Deadline</div>
                <div className="font-mono text-sm text-slate-200 mt-2">
                  {decision.deadline || "—"}
                </div>
              </div>
              <div>
                <div className="label-eyebrow">Stakeholders</div>
                <div className="text-xs text-slate-300 mt-2">
                  {decision.stakeholders?.length
                    ? decision.stakeholders.join(", ")
                    : "—"}
                </div>
              </div>
              <div>
                <div className="label-eyebrow">Created</div>
                <div className="font-mono text-sm text-slate-200 mt-2">
                  {decision.created_at?.slice(0, 10)}
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="space-y-4" data-testid="decision-edit-form">
            <div>
              <label htmlFor="dd-title" className="text-xs text-slate-400 mb-1 block">Title</label>
              <input id="dd-title"
                data-testid="edit-title"
                className="input"
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
              />
            </div>
            <div>
              <label htmlFor="dd-description" className="text-xs text-slate-400 mb-1 block">Description</label>
              <textarea id="dd-description"
                data-testid="edit-description"
                className="input"
                rows={4}
                value={form.description || ""}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label htmlFor="dd-category" className="text-xs text-slate-400 mb-1 block">Category</label>
                <select id="dd-category"
                  data-testid="edit-category"
                  className="input"
                  value={form.category}
                  onChange={(e) => setForm({ ...form, category: e.target.value })}
                >
                  {CATS.map(([k, l]) => (
                    <option key={k} value={k}>
                      {l}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="dd-status" className="text-xs text-slate-400 mb-1 block">Status</label>
                <select id="dd-status"
                  data-testid="edit-status"
                  className="input"
                  value={form.status}
                  onChange={(e) => setForm({ ...form, status: e.target.value })}
                >
                  {STATUSES.map(([k, l]) => (
                    <option key={k} value={k}>
                      {l}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="dd-risk" className="text-xs text-slate-400 mb-1 block">Risk</label>
                <select id="dd-risk"
                  data-testid="edit-risk"
                  className="input"
                  value={form.risk}
                  onChange={(e) => setForm({ ...form, risk: e.target.value })}
                >
                  {RISKS.map(([k, l]) => (
                    <option key={k} value={k}>
                      {l}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="dd-confidence" className="text-xs text-slate-400 mb-1 block">
                  Confidence{" "}
                  <span className="font-mono text-violet-300">{form.confidence}%</span>
                </label>
                <input id="dd-confidence"
                  data-testid="edit-confidence"
                  className="w-full accent-violet-500"
                  type="range"
                  min={0}
                  max={100}
                  value={form.confidence}
                  onChange={(e) =>
                    setForm({ ...form, confidence: Number(e.target.value) })
                  }
                />
              </div>
              <div>
                <label htmlFor="dd-stakeholders-comma-separated" className="text-xs text-slate-400 mb-1 block">
                  Stakeholders (comma separated)
                </label>
                <input id="dd-stakeholders-comma-separated"
                  data-testid="edit-stakeholders"
                  className="input"
                  value={
                    Array.isArray(form.stakeholders)
                      ? form.stakeholders.join(", ")
                      : form.stakeholders || ""
                  }
                  onChange={(e) =>
                    setForm({
                      ...form,
                      stakeholders: e.target.value
                        .split(",")
                        .map((s) => s.trim())
                        .filter(Boolean),
                    })
                  }
                />
              </div>
              <div>
                <label htmlFor="dd-deadline" className="text-xs text-slate-400 mb-1 block">Deadline</label>
                <input id="dd-deadline"
                  data-testid="edit-deadline"
                  type="date"
                  className="input"
                  value={form.deadline || ""}
                  onChange={(e) => setForm({ ...form, deadline: e.target.value })}
                />
              </div>
            </div>
            {err && (
              <div className="text-sm text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-lg px-3 py-2">
                {err}
              </div>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <button
                className="btn btn-secondary"
                onClick={() => {
                  setEditing(false);
                  setForm({ ...decision });
                }}
                data-testid="edit-cancel"
              >
                Cancel
              </button>
              <button
                className="btn btn-primary disabled:opacity-60"
                disabled={saving}
                onClick={saveEdit}
                data-testid="edit-save"
              >
                <Save size={14} /> {saving ? "Saving…" : "Save changes"}
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Attachments */}
        <div className="card p-6" data-testid="attachments-panel">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <Paperclip size={16} className="text-violet-300" />
              <div className="font-sora text-lg font-semibold">Documents</div>
            </div>
            <label
              className="btn btn-secondary cursor-pointer disabled:opacity-60 focus-within:ring-2 focus-within:ring-violet-400"
              data-testid="upload-doc-label"
            >
              <Upload size={14} />
              {uploading ? "Uploading…" : "Upload PDF/DOCX"}
              {/* sr-only, not hidden: `hidden` (display:none) removed the input from the
                  tab order, so the upload control could not be reached by keyboard. */}
              <input
                ref={fileRef}
                type="file"
                accept=".pdf,.docx,.txt,.md"
                className="sr-only"
                disabled={uploading}
                onChange={(e) => uploadFile(e.target.files?.[0])}
                data-testid="upload-doc-input"
              />
            </label>
          </div>
          {uploadLimit && (
            <div
              data-testid="dd-upload-limit"
              className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-100"
            >
              <p>
                Used {uploadLimit.used} of {uploadLimit.limit} documents
                {uploadLimit.reset_at ? " this month" : ""}.
              </p>
              {uploadLimit.upgrade_tier && (
                <Link
                  to="/pricing"
                  className="mt-2 inline-block text-xs font-semibold underline underline-offset-2 hover:text-amber-200"
                  data-testid="dd-upload-limit-upgrade"
                >
                  See upgrade options
                </Link>
              )}
            </div>
          )}
          {attachments.length === 0 && (
            <div className="text-sm text-slate-500 py-6 text-center" data-testid="no-attachments">
              No documents yet. Upload PDF, DOCX, TXT, or MD (max 10 MB).
            </div>
          )}
          <div className="space-y-2">
            {attachments.map((a) => (
              <div
                key={a.id}
                className="flex items-center justify-between p-3 border border-white/5 rounded-lg bg-white/[0.02]"
                data-testid={`attachment-${a.id}`}
              >
                <div className="flex items-center gap-3 min-w-0">
                  <div className="w-9 h-9 rounded-lg bg-violet-600/10 border border-violet-500/20 flex items-center justify-center shrink-0">
                    <FileText size={14} className="text-violet-300" />
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm text-slate-200 truncate">{a.filename}</div>
                    <div className="text-[11px] font-mono text-slate-500">
                      {a.ext.replace(".", "").toUpperCase()} · {fmtBytes(a.size)}
                    </div>
                  </div>
                </div>
                <button
                  onClick={() => deleteAttachment(a.id)}
                  aria-label={`Delete attachment ${a.filename || a.id}`}
                  className="btn btn-ghost !p-1.5 hover:text-rose-400"
                  data-testid={`delete-attachment-${a.id}`}
                >
                  <X size={14} />
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Timeline */}
        <div className="card p-6" data-testid="timeline-panel">
          <div className="flex items-center gap-2 mb-4">
            <Clock size={16} className="text-violet-300" />
            <div className="font-sora text-lg font-semibold">Timeline</div>
          </div>
          <div className="relative pl-6 space-y-4">
            <div className="absolute left-[7px] top-1 bottom-1 w-px bg-white/10" />
            {activity.map((a, i) => (
              <div key={a.id || i} className="relative" data-testid={`timeline-item-${i}`}>
                <div className="absolute -left-[22px] top-0.5 w-3 h-3 rounded-full bg-violet-500 ring-4 ring-violet-500/15" />
                <div className="text-sm text-slate-200">{a.message}</div>
                <div className="text-[11px] font-mono text-slate-500 mt-0.5">
                  {a.created_at?.slice(0, 19).replace("T", " ")}
                </div>
              </div>
            ))}
            {activity.length === 0 && (
              <div className="text-sm text-slate-500">No activity yet.</div>
            )}
          </div>
        </div>
      </div>

      {/* Comments */}
      <div className="card p-6" data-testid="comments-panel">
        <div className="flex items-center gap-2 mb-4">
          <MessageSquare size={16} className="text-violet-300" />
          <div className="font-sora text-lg font-semibold">Comments</div>
        </div>
        <div className="space-y-3 mb-5">
          {comments.length === 0 && (
            <div className="text-sm text-slate-500" data-testid="no-comments">
              Be the first to comment.
            </div>
          )}
          {comments.map((c, i) => (
            <div
              key={c.id}
              className="p-3 border border-white/5 rounded-lg bg-white/[0.02]"
              data-testid={`comment-${i}`}
            >
              <div className="flex items-center justify-between mb-1">
                <div className="text-xs font-semibold text-slate-200">{c.author_name}</div>
                <div className="text-[11px] font-mono text-slate-500">
                  {c.created_at?.slice(0, 16).replace("T", " ")}
                </div>
              </div>
              <div className="text-sm text-slate-300 whitespace-pre-wrap">{c.body}</div>
            </div>
          ))}
        </div>
        <form onSubmit={addComment} className="flex gap-2" data-testid="comment-form">
          <input
            data-testid="comment-input"
            className="input flex-1"
            placeholder="Add a comment…"
            value={newComment}
            onChange={(e) => setNewComment(e.target.value)}
          />
          <button
            type="submit"
            className="btn btn-primary disabled:opacity-60"
            disabled={!newComment.trim()}
            data-testid="comment-submit"
          >
            Post
          </button>
        </form>
      </div>
    </div>
  );
}
