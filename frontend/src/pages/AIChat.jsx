import React, { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api, { formatApiError, limitInfo } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import {
  clearPending,
  newRequestId,
  prunePendingForOtherUsers,
  readPending,
  savePending,
} from "@/lib/chatRecovery";
import { Send, Sparkles, Bot, User, AlertTriangle, RotateCcw } from "lucide-react";
import Markdown from "@/components/Markdown";

const SUGGESTIONS = [
  "Help me structure a build-vs-buy decision for our analytics stack.",
  "What questions should I ask before approving a 6-figure marketing budget?",
  "How do I de-risk a Series B fundraise decision in a tough market?",
  "Outline a pre-mortem for migrating to multi-region.",
];

// How long to keep watching a turn that the server says is already in flight. Gemini's
// own timeout is 60s, so a live worker should finish well inside this window; once it
// elapses the turn is reported as still running and the user can check again.
const POLL_INTERVAL_MS = 1500;
const POLL_DEADLINE_MS = 90000;

// Server-side failure codes, in words a user can act on. The codes themselves stay out of
// the UI: they are a contract, not copy.
const ERROR_COPY = {
  llm_unavailable: "The AI service could not be reached. Please try again.",
  llm_empty_response: "The AI service returned an empty response. Please try again.",
  persistence_failed: "The reply could not be saved. Please try again.",
  attempt_abandoned: "This answer was interrupted before it finished.",
  retry_exhausted:
    "This message has used all of its retries. Send it again as a new message to keep going.",
  request_unrecoverable: "This answer is no longer available. Send the message again.",
};

const GENERIC_ERROR = "Failed to reach the AI service. Please try again.";
const CONFLICT_COPY =
  "This message changed while it was being sent, so it could not be matched to the original request. Send it again.";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/*
 * The request lifecycle below is deliberately outside the component.
 *
 * Each request carries a client-generated `request_id`. Resending that same id is how the
 * client asks "what happened to my message?" instead of submitting it twice: the server
 * replays a finished answer, refuses a conflicting payload, or reports that the turn is
 * still running. Nothing here decides idempotency locally -- the server owns that -- these
 * helpers only interpret its answers.
 *
 * Every helper resolves to one outcome shape so the UI has a single place to branch:
 *   { kind: "ok", sessionId, reply }
 *   { kind: "failed", code, canRetry, attempt, maxAttempts }
 *   { kind: "limit", limit }
 *   { kind: "conflict" }
 *   { kind: "timeout" }            still running when we stopped watching
 *   { kind: "missing" }            the server has no record of this request id
 *   { kind: "error", message, canRetry }
 */

function outcomeFromStatus(status) {
  if (!status) return null;

  if (status.status === "answered") {
    // An answered row with no reply is a broken pair, not a success. Rendering nothing and
    // waiting would hang the turn, so report it as a terminal failure instead.
    if (status.reply) {
      return { kind: "ok", sessionId: status.session_id, reply: status.reply };
    }
    return { kind: "failed", code: "request_unrecoverable", canRetry: false };
  }

  if (status.status === "failed") {
    return {
      kind: "failed",
      code: status.error_code || "llm_unavailable",
      canRetry: Boolean(status.can_retry),
      attempt: status.attempt,
      maxAttempts: status.max_attempts,
    };
  }

  return null; // pending or processing: not resolved yet
}

function errorOutcome(e) {
  if (!e.response) {
    return {
      kind: "error",
      message: "Could not reach the server. Check your connection and try again.",
      canRetry: true,
    };
  }
  if (e.response.status === 401) {
    return {
      kind: "error",
      message: "Your session expired. Sign in again to continue.",
      canRetry: false,
    };
  }
  return {
    kind: "error",
    message: formatApiError(e.response.data?.detail) || GENERIC_ERROR,
    canRetry: true,
  };
}

// One POST. A 409 for an in-flight turn means another worker owns this request id, so the
// only correct follow-up is to watch it -- resubmitting would risk a second dispatch.
async function submitTurn({ message, requestId, sessionId }) {
  try {
    const r = await api.post("/chat", {
      message,
      session_id: sessionId || null,
      request_id: requestId,
    });
    return { kind: "ok", sessionId: r.data.session_id, reply: r.data.reply };
  } catch (e) {
    const status = e.response?.status;
    const detail = e.response?.data?.detail;
    const code = detail && typeof detail === "object" ? detail.code : null;

    if (status === 409 && code === "request_in_flight") {
      return watchRequest(requestId);
    }
    if (status === 409 && code === "idempotency_conflict") {
      return { kind: "conflict" };
    }
    if (status === 409 && code === "retry_exhausted") {
      return {
        kind: "failed",
        code: "retry_exhausted",
        canRetry: false,
        attempt: detail.attempt,
        maxAttempts: detail.max_attempts,
      };
    }
    if (status === 409 && code === "request_unrecoverable") {
      return { kind: "failed", code: "request_unrecoverable", canRetry: false };
    }

    const limit = limitInfo(e);
    if (limit) return { kind: "limit", limit };

    return errorOutcome(e);
  }
}

// A read-only poll of the request status endpoint, used both after a 409 and after a reload.
async function watchRequest(requestId) {
  const deadline = Date.now() + POLL_DEADLINE_MS;
  for (;;) {
    await sleep(POLL_INTERVAL_MS);
    try {
      const status = (await api.get(`/chat/requests/${requestId}`)).data;
      const outcome = outcomeFromStatus(status);
      if (outcome) return outcome;
    } catch (e) {
      if (e.response?.status === 401) return errorOutcome(e);
      // A 404 or a blip while the row demonstrably existed moments ago: keep watching until
      // the deadline rather than declaring the turn lost.
    }
    if (Date.now() >= deadline) return { kind: "timeout" };
  }
}

// A single read, for reconciling a message that was saved before the tab reloaded. Never
// dispatches: a message that failed must not silently retry itself on every page load.
async function lookupOnce(requestId) {
  try {
    const status = (await api.get(`/chat/requests/${requestId}`)).data;
    return outcomeFromStatus(status) || { kind: "pending" };
  } catch (e) {
    if (e.response?.status === 404) return { kind: "missing" };
    return errorOutcome(e);
  }
}

export default function AIChat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef(null);
  const navigate = useNavigate();
  const { user } = useAuth();

  // Pending request bookkeeping is scoped to the signed-in account. Without an id there is
  // no safe scope, so nothing is stored or replayed.
  const userId = user && user.id ? user.id : null;

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  const applyOutcome = useCallback(
    (outcome, ctx) => {
      // A retry replaces the previous failure for the same request id, so repeated attempts
      // never pile up error bubbles.
      const withoutThisError = (list) =>
        list.filter((m) => !(m.role === "error" && m.requestId === ctx.requestId));

      if (outcome.kind === "ok") {
        clearPending(userId);
        setSessionId(outcome.sessionId);
        setMessages((m) => [
          ...withoutThisError(m),
          { role: "assistant", content: outcome.reply, key: `a-${ctx.requestId}` },
        ]);
        return;
      }

      if (outcome.kind === "limit") {
        // The message was never accepted, so there is nothing to recover.
        clearPending(userId);
        setMessages((m) => [
          ...withoutThisError(m),
          {
            role: "limit",
            content: outcome.limit.message,
            limit: outcome.limit,
            key: `l-${ctx.requestId}`,
          },
        ]);
        return;
      }

      if (outcome.kind === "conflict") {
        clearPending(userId);
        setMessages((m) => [
          ...withoutThisError(m),
          {
            role: "error",
            content: CONFLICT_COPY,
            key: `e-${ctx.requestId}`,
            requestId: ctx.requestId,
            canRetry: false,
          },
        ]);
        return;
      }

      // "timeout" and "missing" are both recoverable by sending the same id again; a
      // server-reported failure decides for itself via can_retry (the attempt budget).
      const canRetry =
        outcome.kind === "timeout" || outcome.kind === "missing"
          ? true
          : Boolean(outcome.canRetry);

      if (!canRetry) clearPending(userId);

      const content =
        outcome.message ||
        ERROR_COPY[outcome.code] ||
        (outcome.kind === "timeout"
          ? "This answer is taking longer than expected. Check again to pick it up."
          : GENERIC_ERROR);

      setMessages((m) => [
        ...withoutThisError(m),
        {
          role: "error",
          content,
          key: `e-${ctx.requestId}`,
          requestId: ctx.requestId,
          sourceMessage: ctx.message,
          sessionId: ctx.sessionId,
          canRetry,
          attempt: outcome.attempt,
          maxAttempts: outcome.maxAttempts,
        },
      ]);
    },
    [userId],
  );

  // Reconcile a submission that outlived the page. The status is read first so an outcome
  // that is already settled costs no provider call; only an unsettled turn is handed back
  // to the server, which is what makes a reload safe rather than a duplicate submission.
  useEffect(() => {
    if (!userId) return undefined;

    // A key belonging to another account must never survive on a shared tab.
    prunePendingForOtherUsers(userId);
    const saved = readPending(userId);
    if (!saved) return undefined;

    let cancelled = false;
    const ctx = {
      requestId: saved.requestId,
      message: saved.message,
      sessionId: saved.sessionId || null,
    };

    // Restore what the user saw, so an interrupted turn is not silently swallowed.
    setMessages([{ role: "user", content: ctx.message, key: ctx.requestId }]);
    if (ctx.sessionId) setSessionId(ctx.sessionId);
    setLoading(true);

    (async () => {
      const outcome = await lookupOnce(ctx.requestId);
      if (cancelled) return;

      // Terminal outcomes (answered, failed, conflict) are shown as they are: a message that
      // already failed must not spend another attempt just because the page was reloaded.
      // An unsettled one is handed back to the server with the SAME request id, because a
      // read cannot advance it: the server resumes a turn whose worker died, and answers
      // 409 `request_in_flight` for one that is genuinely still running, which we then
      // watch. Either way no duplicate work is launched.
      let settled = outcome;
      if (outcome.kind === "pending" || outcome.kind === "missing") {
        settled = await submitTurn(ctx);
        if (cancelled) return;
      }

      applyOutcome(settled, ctx);
      if (!cancelled) setLoading(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [userId, applyOutcome]);

  async function attempt(ctx) {
    if (!userId) return;
    setLoading(true);
    // Saved before the request is sent: if the tab dies mid-flight the id survives, and the
    // next visit can ask the server what happened instead of resending blind.
    savePending(userId, {
      requestId: ctx.requestId,
      message: ctx.message,
      sessionId: ctx.sessionId ?? null,
    });
    try {
      const outcome = await submitTurn(ctx);
      applyOutcome(outcome, ctx);
    } finally {
      // The composer must never stay locked because of an unexpected throw.
      setLoading(false);
    }
  }

  function send(message) {
    if (!message.trim() || loading) return;
    const requestId = newRequestId();
    setMessages((m) => [...m, { role: "user", content: message, key: requestId }]);
    setInput("");
    attempt({ requestId, message, sessionId });
  }

  // Retry reuses the original request id, so the server continues the same user message
  // instead of creating a duplicate one.
  function retry(m) {
    if (loading || !m.requestId) return;
    attempt({
      requestId: m.requestId,
      message: m.sourceMessage,
      sessionId: m.sessionId,
    });
  }

  function onSubmit(e) {
    e.preventDefault();
    send(input);
  }

  return (
    <div className="h-[calc(100vh-8rem)] flex flex-col" data-testid="ai-chat-page">
      <div className="flex items-end justify-between mb-4">
        <div>
          <div className="label-eyebrow">Copilot</div>
          <h1 className="font-sora text-3xl font-semibold mt-1 flex items-center gap-2">
            <Sparkles size={22} className="text-violet-300" />
            AI Chat
          </h1>
          <p className="text-slate-400 text-sm mt-1">
            Powered by Gemini 2.5 · Multi-turn session
          </p>
        </div>
        <div className="text-xs font-mono text-slate-500">
          {sessionId ? `session: ${sessionId.slice(0, 8)}` : "new session"}
        </div>
      </div>

      <div className="card flex-1 flex flex-col overflow-hidden">
        <div
          ref={scrollRef}
          className="flex-1 overflow-y-auto p-6 space-y-4"
          data-testid="chat-messages"
        >
          {messages.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center text-center max-w-md mx-auto">
              <div className="w-14 h-14 rounded-2xl bg-violet-600/10 border border-violet-500/30 flex items-center justify-center mb-4 glow-violet">
                <Sparkles size={22} className="text-violet-300" />
              </div>
              <h3 className="font-sora text-lg font-semibold">
                Ask DecisionLens AI anything.
              </h3>
              <p className="text-slate-400 text-sm mt-1 mb-6">
                Frame decisions, run pre-mortems, get critique. Try a starter ↓
              </p>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2 w-full">
                {SUGGESTIONS.map((s, i) => (
                  <button
                    key={i}
                    onClick={() => send(s)}
                    className="card p-3 text-left text-sm text-slate-300 hover:border-violet-500/30 transition-colors"
                    data-testid={`chat-suggestion-${i}`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div
              key={m.key || i}
              className={`flex gap-3 ${
                m.role === "user" ? "justify-end" : "justify-start"
              }`}
              data-testid={`chat-msg-${i}`}
            >
              {(m.role === "assistant" || m.role === "error" || m.role === "limit") && (
                <div
                  className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${
                    m.role === "error"
                      ? "bg-red-500/15 border border-red-500/30"
                      : m.role === "limit"
                        ? "bg-amber-500/15 border border-amber-500/30"
                        : "bg-violet-600/15 border border-violet-500/30"
                  }`}
                >
                  {m.role === "error" ? (
                    <AlertTriangle size={14} className="text-red-300" />
                  ) : m.role === "limit" ? (
                    <AlertTriangle size={14} className="text-amber-300" />
                  ) : (
                    <Bot size={14} className="text-violet-300" />
                  )}
                </div>
              )}
              <div
                className={`max-w-[85%] sm:max-w-[75%] rounded-xl px-4 py-3 text-sm leading-relaxed ${
                  m.role === "user"
                    ? "bg-violet-600 text-white"
                    : m.role === "error"
                      ? "bg-red-500/10 border border-red-500/30 text-red-200"
                      : m.role === "limit"
                        ? "bg-amber-500/10 border border-amber-500/30 text-amber-100"
                        : "bg-slate-800/70 border border-white/5 text-slate-200"
                }`}
              >
                {m.role === "assistant" ? (
                  <Markdown>{m.content}</Markdown>
                ) : (
                  <span className="whitespace-pre-wrap break-words">
                    {m.content}
                    {m.role === "limit" && m.limit?.upgrade_tier && (
                      <button
                        onClick={() => navigate("/pricing")}
                        className="mt-2 block text-xs font-semibold underline underline-offset-2 hover:text-amber-200"
                      >
                        See upgrade options
                      </button>
                    )}
                    {m.role === "error" && m.canRetry && (
                      <button
                        onClick={() => retry(m)}
                        disabled={loading}
                        className="mt-2 flex items-center gap-1.5 text-xs font-semibold underline underline-offset-2 hover:text-red-100 disabled:opacity-50"
                        data-testid="chat-retry"
                      >
                        <RotateCcw size={12} /> Retry this message
                      </button>
                    )}
                  </span>
                )}
              </div>
              {m.role === "user" && (
                <div className="w-8 h-8 rounded-lg bg-blue-600/15 border border-blue-500/30 flex items-center justify-center shrink-0">
                  <User size={14} className="text-blue-300" />
                </div>
              )}
            </div>
          ))}
          {loading && (
            <div className="flex gap-3" data-testid="chat-loading">
              <div className="w-8 h-8 rounded-lg bg-violet-600/15 border border-violet-500/30 flex items-center justify-center shrink-0">
                <Bot size={14} className="text-violet-300" />
              </div>
              <div className="bg-slate-800/70 border border-white/5 rounded-xl px-4 py-3 text-sm text-slate-400">
                <span className="inline-flex gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
                  <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse [animation-delay:200ms]" />
                  <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse [animation-delay:400ms]" />
                </span>
              </div>
            </div>
          )}
        </div>

        <form
          onSubmit={onSubmit}
          className="border-t border-white/5 p-4 flex gap-2 bg-slate-900/40"
          data-testid="chat-form"
        >
          <input
            data-testid="chat-input"
            className="input flex-1"
            placeholder="Ask DecisionLens AI…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={loading}
          />
          <button
            type="submit"
            disabled={loading || !input.trim()}
            className="btn btn-primary disabled:opacity-60"
            data-testid="chat-send"
          >
            <Send size={14} /> Send
          </button>
        </form>
      </div>
    </div>
  );
}
