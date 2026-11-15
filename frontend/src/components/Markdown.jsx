import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Shared Markdown renderer.
 *
 * Builds React elements rather than injecting an HTML string, so it keeps the app's
 * "no dangerouslySetInnerHTML anywhere" property.
 *
 * Single newlines collapse into flowing text, while blank lines still start a new
 * paragraph. That is why plain prose pasted with hard line breaks reads correctly here
 * instead of rendering as a ragged narrow column.
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
  ul: ({ children }) => (
    <ul className="my-2 pl-4 list-disc space-y-1 marker:text-slate-500">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 pl-4 list-decimal space-y-1 marker:text-slate-500">{children}</ol>
  ),
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
  em: ({ children }) => <em className="italic text-slate-100">{children}</em>,
  del: ({ children }) => <del className="text-slate-400">{children}</del>,
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-violet-300 underline underline-offset-2 hover:text-violet-200"
    >
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
  td: ({ children }) => (
    <td className="border border-white/10 px-2 py-1 align-top">{children}</td>
  ),
};

export default function Markdown({ children, className = "" }) {
  if (!children || !String(children).trim()) return null;
  return (
    <div className={`text-[13px] leading-relaxed break-words ${className}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD}>
        {children}
      </ReactMarkdown>
    </div>
  );
}

export { MD as markdownComponents };