import React from "react";
import { Brain, Sparkles, Shield, Activity } from "lucide-react";
import Logo from "@/components/Logo";

const FEATURES = [
  { icon: Brain, title: "Decision Health Score", body: "Our signature metric scores every decision across 4 factors." },
  { icon: Sparkles, title: "AI Copilot", body: "Multi-turn AI critique powered by Gemini 2.5." },
  { icon: Shield, title: "Risk Mapping", body: "Visualize risk distribution and reduce exposure." },
  { icon: Activity, title: "Outcome Tracking", body: "Revisit completed decisions to learn what actually worked." },
];

export default function About() {
  return (
    <div className="space-y-8 max-w-3xl" data-testid="about-page">
      <div>
        <Logo variant="full" className="h-48 w-auto mb-6" alt="DecisionLens AI" />
        <div className="label-eyebrow">About</div>
        <h1 className="font-sora text-3xl font-semibold mt-1">DecisionLens AI</h1>
        <p className="text-slate-400 text-sm mt-2 leading-relaxed">
          DecisionLens AI is a decision intelligence cockpit for leadership teams. We turn
          ad-hoc judgement calls into a tracked, scored, defensible portfolio of choices — with
          AI critique built in.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {FEATURES.map((f) => {
          const Icon = f.icon;
          return (
            <div key={f.title} className="card p-5">
              <div className="w-10 h-10 rounded-lg bg-violet-600/10 border border-violet-500/30 flex items-center justify-center mb-3">
                <Icon size={16} className="text-violet-300" />
              </div>
              <div className="font-sora text-base font-semibold">{f.title}</div>
              <p className="text-sm text-slate-400 mt-1">{f.body}</p>
            </div>
          );
        })}
      </div>

      <div className="card p-6">
        <div className="grid grid-cols-3 gap-4 text-center">
          <div>
            <div className="metric-num text-2xl">v1.0</div>
            <div className="label-eyebrow mt-1">Version</div>
          </div>
          <div>
            <div className="metric-num text-2xl">Gemini 2.5</div>
            <div className="label-eyebrow mt-1">AI Model</div>
          </div>
          <div>
            <div className="metric-num text-2xl">2026</div>
            <div className="label-eyebrow mt-1">Released</div>
          </div>
        </div>
      </div>

      <div className="text-[11px] font-mono text-slate-600 tracking-widest uppercase">
        © 2026 DecisionLens AI
      </div>
    </div>
  );
}
