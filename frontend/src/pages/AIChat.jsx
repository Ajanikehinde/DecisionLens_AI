import React, { useEffect, useRef, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { Send, Sparkles, Bot, User, AlertTriangle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const SUGGESTIONS = [
  "Help me structure a build-vs-buy decision for our analytics stack.",
  "What questions should I ask before approving a 6-figure marketing budget?",
  "How do I de-risk a Series B fundraise decision in a tough market?",
  "Outline a pre-mortem for migrating to multi-region.",
];

/**
 * Element overrides so model output matches the app's type scale and spacing.
 * react-markdown maps markdown tags to these components.
 */
const MD = {
  h1: ({ children }) => (
    <h3 className="font-sora text-base font-semibold mt-4 mb-2 first:mt-0">{children}</h3>
  ),
  h2: ({ children }) => (
    <h3 className="font-sora text-base font-semibold mt-4 mb-2 first:mt-0">{children}</h3>
  ),
  h3: ({ children }) => (
    <h4 className="font-sora text-[15px] font-semibold text-violet-200 mt-3 mb-1.5 first:mt-0">
      {children}
    </h4>
  ),
  h4: ({ children }) => <p className="font-semibold mt-3 mb-1 first:mt-0">{children}</p>,
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-2 pl-4 list-disc space-y-1 marker:text-slate-500">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 pl-4 list-decimal space-y-1 marker:text-slate-500">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
  em: ({ children }) => <em className="italic text-slate-100">{children}</em>,
  del: ({ children }) => <del className="text-slate-400">{children}</del>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer" className="text-violet-300 underline underline-offset-2 hover:text-violet-200">
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 border-violet-500/50 pl-3 text-slate-300 italic">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-3 border-white/10" />,
  code: ({ inline, children }) =>
    inline ? (
      <code className="px-1 py-0.5 rounded bg-black/40 font-mono text-[12px] text-emerald-300">
        {children}
      </code>
    ) : (
      <code className="block font-mono text-[12px] leading-relaxed">{children}</code>
    ),
  pre: ({ children }) => (
    <pre className="my-2 p-3 rounded-lg bg-black/50 border border-white/10 overflow-x-auto">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="w-full text-[13px] border-collapse">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-white/10 bg-black/30 px-2 py-1 text-left font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => <td className="border border-white/10 px-2 py-1 align-top">{children}</td>,
};

export default function AIChat() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  async function send(message) {
    if (!message.trim() || loading) return;
    setMessages((m) => [...m, { role: "user", content: message }]);
    setInput("");
    setLoading(true);
    try {
      const r = await api.post("/chat", { message, session_id: sessionId });
      setSessionId(r.data.session_id);
      setMessages((m) => [...m, { role: "assistant", content: r.data.reply }]);
    } catch (e) {
      // Rendered as a distinct error bubble, never as an assistant message, so a
      // failed request can never be mistaken for a real model reply.
      setMessages((m) => [
        ...m,
        {
          role: "error",
          content:
            formatApiError(e.response?.data?.detail) ||
            "Failed to reach the AI service. Please try again.",
        },
      ]);
    } finally {
      setLoading(false);
    }
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
              key={i}
              className={`flex gap-3 ${
                m.role === "user" ? "justify-end" : "justify-start"
              }`}
              data-testid={`chat-msg-${i}`}
            >
              {(m.role === "assistant" || m.role === "error") && (
                <div
                  className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${
                    m.role === "error"
                      ? "bg-red-500/15 border border-red-500/30"
                      : "bg-violet-600/15 border border-violet-500/30"
                  }`}
                >
                  {m.role === "error" ? (
                    <AlertTriangle size={14} className="text-red-300" />
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
                      : "bg-slate-800/70 border border-white/5 text-slate-200"
                }`}
              >
                {m.role === "assistant" ? (
                  <div className="text-[13px] leading-relaxed break-words">
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD}>
                      {m.content}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <span className="whitespace-pre-wrap break-words">{m.content}</span>
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
