import React, { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError, limitInfo } from "@/lib/api";
import { Save, Upload, FileText, X } from "lucide-react";

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

function fmtBytes(b) {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}

export default function NewDecision() {
  const navigate = useNavigate();
  const fileRef = useRef(null);
  const [form, setForm] = useState({
    title: "",
    description: "",
    category: "business_strategy",
    risk: "medium",
    status: "active",
    confidence: 75,
    stakeholders: "",
    deadline: "",
  });
  const [pendingFiles, setPendingFiles] = useState([]);
  const [err, setErr] = useState("");
  const [limit, setLimit] = useState(null);
  const [saving, setSaving] = useState(false);
  const [dragging, setDragging] = useState(false);

  function update(k, v) {
    setForm({ ...form, [k]: v });
  }

  function addFiles(list) {
    const allowed = [".pdf", ".docx", ".txt", ".md"];
    const cleaned = [];
    for (const f of list) {
      const ext = "." + (f.name.split(".").pop() || "").toLowerCase();
      if (!allowed.includes(ext)) {
        setErr(`Unsupported file ${f.name}. Allowed: PDF, DOCX, TXT, MD`);
        continue;
      }
      if (f.size > 10 * 1024 * 1024) {
        setErr(`File ${f.name} exceeds 10 MB limit`);
        continue;
      }
      cleaned.push(f);
    }
    if (cleaned.length) setErr("");
    setPendingFiles((p) => [...p, ...cleaned]);
  }

  function onFilePicked(e) {
    if (e.target.files?.length) addFiles(Array.from(e.target.files));
    if (fileRef.current) fileRef.current.value = "";
  }

  function onDrop(e) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files?.length) addFiles(Array.from(e.dataTransfer.files));
  }

  function removePending(idx) {
    setPendingFiles((p) => p.filter((_, i) => i !== idx));
  }

  async function submit(e) {
    e.preventDefault();
    setErr("");
    setLimit(null);
    setSaving(true);

    let newId = null;
    try {
      const r = await api.post("/decisions", {
        ...form,
        confidence: Number(form.confidence),
        stakeholders: form.stakeholders
          ? form.stakeholders.split(",").map((s) => s.trim()).filter(Boolean)
          : [],
        deadline: form.deadline || null,
      });
      newId = r.data.id;
    } catch (e) {
      // The decision was rejected, so nothing was written at all.
      setLimit(limitInfo(e));
      setErr(formatApiError(e.response?.data?.detail) || e.message);
      setSaving(false);
      return;
    }

    // From here the decision exists. An upload quota wall must not strand it, so the
    // user is still sent to the decision and told which files did not make it.
    const rejected = [];
    for (const f of pendingFiles) {
      const fd = new FormData();
      fd.append("file", f);
      try {
        await api.post(`/decisions/${newId}/attachments`, fd, {
          headers: { "Content-Type": "multipart/form-data" },
        });
      } catch (e) {
        rejected.push(f.name);
        setLimit(limitInfo(e));
        break;
      }
    }
    if (rejected.length) {
      setErr(
        `Decision saved, but these files were not uploaded: ${rejected.join(", ")}. ` +
          `You can add them from the decision page.`,
      );
    }
    setSaving(false);
    navigate(`/decisions/${newId}`);
  }

  return (
    <div className="max-w-3xl mx-auto space-y-6" data-testid="new-decision-page">
      <div>
        <div className="label-eyebrow">Capture</div>
        <h1 className="font-sora text-3xl font-semibold mt-1">New Decision</h1>
        <p className="text-slate-400 text-sm mt-1">
          Frame the decision, attach supporting docs, and score your initial confidence.
        </p>
      </div>

      <form onSubmit={submit} className="card p-6 space-y-5" data-testid="new-decision-form">
        <div>
          <label htmlFor="nd-title-2" className="text-xs text-slate-400 mb-1 block">Title *</label>
          <input id="nd-title-2"
            data-testid="nd-title"
            className="input"
            required
            value={form.title}
            onChange={(e) => update("title", e.target.value)}
            placeholder="e.g. Launch enterprise tier in Q2"
          />
        </div>

        <div>
          <label htmlFor="nd-description-context" className="text-xs text-slate-400 mb-1 block">Description / Context</label>
          <textarea id="nd-description-context"
            data-testid="nd-description"
            className="input"
            rows={4}
            value={form.description}
            onChange={(e) => update("description", e.target.value)}
            placeholder="What is being decided? What outcome are we trying to drive?"
          />
        </div>

        {/* Document upload */}
        <div>
          <label htmlFor="nd-supporting-documents-pdf-docx-txt-md-" className="text-xs text-slate-400 mb-1 block">
            Supporting Documents <span className="text-slate-600">(PDF, DOCX, TXT, MD — max 10 MB each)</span>
          </label>
          <div
            role="button"
            tabIndex={0}
            aria-label="Add documents: drop files here or browse"
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                fileRef.current?.click();
              }
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => fileRef.current?.click()}
            className={`border border-dashed rounded-xl p-6 text-center cursor-pointer transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-violet-400 ${
              dragging
                ? "border-violet-500 bg-violet-500/10"
                : "border-white/10 hover:border-violet-500/40 hover:bg-white/[0.02]"
            }`}
            data-testid="nd-dropzone"
          >
            <div className="w-10 h-10 mx-auto rounded-lg bg-violet-600/10 border border-violet-500/20 flex items-center justify-center mb-2">
              <Upload size={16} className="text-violet-300" />
            </div>
            <div className="text-sm text-slate-300">
              Drop files here or <span className="text-violet-300 font-semibold">browse</span>
            </div>
            <div className="text-[11px] font-mono text-slate-500 mt-1">
              PDF · DOCX · TXT · MD
            </div>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.txt,.md"
              multiple
              className="hidden"
              onChange={onFilePicked}
              data-testid="nd-file-input"
             id="nd-supporting-documents-pdf-docx-txt-md-" />
          </div>

          {pendingFiles.length > 0 && (
            <div className="mt-3 space-y-2" data-testid="nd-pending-files">
              {pendingFiles.map((f, i) => (
                <div
                  key={i}
                  className="flex items-center justify-between p-2 pl-3 border border-white/5 rounded-lg bg-white/[0.02]"
                  data-testid={`nd-pending-${i}`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <FileText size={14} className="text-violet-300 shrink-0" />
                    <span className="text-sm text-slate-200 truncate">{f.name}</span>
                    <span className="text-[10px] font-mono text-slate-500 shrink-0">
                      {fmtBytes(f.size)}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => removePending(i)}
                    aria-label={`Remove ${f.name}`}
                    className="btn btn-ghost !p-1.5"
                    data-testid={`nd-remove-${i}`}
                  >
                    <X size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label htmlFor="nd-category-2" className="text-xs text-slate-400 mb-1 block">Category</label>
            <select id="nd-category-2"
              data-testid="nd-category"
              className="input"
              value={form.category}
              onChange={(e) => update("category", e.target.value)}
            >
              {CATS.map(([k, l]) => (
                <option key={k} value={k}>{l}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="nd-status-2" className="text-xs text-slate-400 mb-1 block">Status</label>
            <select id="nd-status-2"
              data-testid="nd-status"
              className="input"
              value={form.status}
              onChange={(e) => update("status", e.target.value)}
            >
              {STATUSES.map(([k, l]) => (
                <option key={k} value={k}>{l}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="nd-risk-2" className="text-xs text-slate-400 mb-1 block">Risk</label>
            <select id="nd-risk-2"
              data-testid="nd-risk"
              className="input"
              value={form.risk}
              onChange={(e) => update("risk", e.target.value)}
            >
              {RISKS.map(([k, l]) => (
                <option key={k} value={k}>{l}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="nd-confidence-2" className="text-xs text-slate-400 mb-1 block">
              Confidence <span className="font-mono text-violet-300">{form.confidence}%</span>
            </label>
            <input id="nd-confidence-2"
              data-testid="nd-confidence"
              className="w-full accent-violet-500"
              type="range"
              min={0}
              max={100}
              value={form.confidence}
              onChange={(e) => update("confidence", e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="nd-stakeholders-comma-separated" className="text-xs text-slate-400 mb-1 block">Stakeholders (comma-separated)</label>
            <input id="nd-stakeholders-comma-separated"
              data-testid="nd-stakeholders"
              className="input"
              value={form.stakeholders}
              onChange={(e) => update("stakeholders", e.target.value)}
              placeholder="CEO, CFO, Head of Product"
            />
          </div>
          <div>
            <label htmlFor="nd-deadline-2" className="text-xs text-slate-400 mb-1 block">Deadline</label>
            <input id="nd-deadline-2"
              data-testid="nd-deadline"
              type="date"
              className="input"
              value={form.deadline}
              onChange={(e) => update("deadline", e.target.value)}
            />
          </div>
        </div>

        {err && (
          <div
            className="text-sm text-rose-400 bg-rose-500/10 border border-rose-500/20 rounded-lg px-3 py-2"
            data-testid="nd-error"
          >
            {err}
          </div>
        )}

        {limit && (
          <div
            data-testid="nd-limit"
            className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-100"
          >
            <p>
              Used {limit.used} of {limit.limit} {limit.resource === "documents" ? "documents" : "decisions"}
              {limit.reset_at ? ` this month` : ""}.
            </p>
            {limit.upgrade_tier && (
              <button
                type="button"
                onClick={() => navigate("/pricing")}
                className="mt-2 text-xs font-semibold underline underline-offset-2 hover:text-amber-200"
              >
                See upgrade options
              </button>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => navigate("/decisions")}
            data-testid="nd-cancel"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving}
            className="btn btn-primary disabled:opacity-60"
            data-testid="nd-submit"
          >
            <Save size={14} /> {saving ? "Saving…" : "Save Decision"}
          </button>
        </div>
      </form>
    </div>
  );
}
