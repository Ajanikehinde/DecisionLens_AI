import React, { useEffect, useState } from "react";
import { Outlet, NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  ListChecks,
  PlusCircle,
  Sparkles,
  BarChart3,
  Database,
  Settings,
  Info,
  Search,
  LogOut,
  Menu,
  X,
  Sparkle,
  Crown,
} from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import Logo from "@/components/Logo";
import api from "@/lib/api";

const navItems = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, tid: "nav-dashboard", end: true },
  { to: "/decisions", label: "Decisions", icon: ListChecks, tid: "nav-decisions" },
  { to: "/decisions/new", label: "New Decision", icon: PlusCircle, tid: "nav-new-decision" },
  { to: "/chat", label: "AI Chat", icon: Sparkles, tid: "nav-ai-chat" },
  { to: "/analytics", label: "Analytics", icon: BarChart3, tid: "nav-analytics" },
  { to: "/sources", label: "Sources", icon: Database, tid: "nav-sources" },
  { to: "/pricing", label: "Pricing", icon: Crown, tid: "nav-pricing" },
  { to: "/settings", label: "Settings", icon: Settings, tid: "nav-settings" },
  { to: "/about", label: "About", icon: Info, tid: "nav-about" },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [tier, setTier] = useState(null);
  const [navOpen, setNavOpen] = useState(false);

  // Close the mobile drawer on Escape and when the viewport grows past the breakpoint.
  useEffect(() => {
    if (!navOpen) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    const mq = window.matchMedia("(min-width: 1024px)");
    const onChange = (e) => {
      if (e.matches) setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    mq.addEventListener("change", onChange);
    return () => {
      window.removeEventListener("keydown", onKey);
      mq.removeEventListener("change", onChange);
    };
  }, [navOpen]);

  // Any navigation closes the drawer.
  useEffect(() => {
    setNavOpen(false);
  }, [user?.id]);

  React.useEffect(() => {
    api.get("/billing/me").then((r) => setTier(r.data)).catch(() => {});
  }, []);

  async function onLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <div className="min-h-screen flex bg-grid" data-testid="app-layout">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-2 focus:rounded focus:bg-violet-600 focus:px-3 focus:py-2 focus:text-sm focus:text-white"
      >
        Skip to main content
      </a>

      {/* Mobile drawer scrim */}
      {navOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/60 lg:hidden"
          onClick={() => setNavOpen(false)}
          data-testid="nav-scrim"
          aria-hidden="true"
        />
      )}

      {/* Sidebar: off-canvas below lg, static from lg up */}
      <aside
        data-testid="sidebar"
        className={`fixed inset-y-0 left-0 z-40 w-64 shrink-0 transform border-r border-white/5 bg-[#0b1224] px-5 py-6 flex flex-col transition-transform duration-200 lg:static lg:translate-x-0 lg:bg-[#0b1224]/60 lg:backdrop-blur-xl ${
          navOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center gap-2 mb-8">
          <Logo variant="mark" className="w-9 h-9 shrink-0" />
          <div>
            <div className="font-sora text-[15px] font-semibold tracking-tight text-white">
              DecisionLens
            </div>
            <div className="font-mono text-[10px] tracking-widest uppercase text-slate-500">
              AI · v1.0
            </div>
          </div>
          <button
            type="button"
            onClick={() => setNavOpen(false)}
            className="ml-auto btn btn-ghost !p-1.5 lg:hidden"
            aria-label="Close navigation"
            data-testid="nav-close"
          >
            <X size={16} />
          </button>
        </div>

        <div className="label-eyebrow mb-2 pl-1">Navigation</div>
        <nav className="flex flex-col gap-0.5 pl-2">
          {navItems.map(({ to, label, icon: Icon, tid, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              data-testid={tid}
              className={({ isActive }) => `sidebar-link ${isActive ? "active" : ""}`}
            >
              <Icon size={16} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto pt-6">
          <div className="card p-3 flex items-center gap-3">
            <div className="w-9 h-9 rounded-full bg-gradient-to-br from-violet-500 to-blue-500 flex items-center justify-center text-white font-mono text-sm">
              {(user?.name || "U")[0].toUpperCase()}
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-sm text-white truncate" data-testid="sidebar-user-name">
                {user?.name}
              </div>
              <div className="text-[11px] text-slate-500 truncate font-mono">{user?.email}</div>
            </div>
            <button
              data-testid="logout-btn"
              onClick={onLogout}
              className="btn btn-ghost !p-1.5"
              title="Sign out"
              aria-label="Sign out"
            >
              <LogOut size={14} />
            </button>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main id="main-content" className="flex-1 min-w-0 flex flex-col">
        <header
          data-testid="topbar"
          className="h-16 border-b border-white/5 px-4 sm:px-8 flex items-center gap-4 bg-[#0b1224]/40 backdrop-blur-xl sticky top-0 z-10"
        >
          <button
            type="button"
            onClick={() => setNavOpen(true)}
            className="btn btn-ghost !p-2 lg:hidden"
            aria-label="Open navigation"
            aria-expanded={navOpen}
            data-testid="nav-toggle"
          >
            <Menu size={18} />
          </button>
          <div className="relative flex-1 max-w-xl">
            <Search
              size={15}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500"
            />
<input
              data-testid="topbar-search"
              aria-label="Search decisions, sources, anything"
              placeholder="Search decisions, sources, anything."
              className="input pl-9 !py-2"
            />
            <span className="hidden md:flex absolute right-2 top-1/2 -translate-y-1/2 font-mono text-[10px] text-slate-500 border border-white/10 rounded px-1.5 py-0.5">
              ?K
            </span>
          </div>
          {tier && (
            <NavLink
              to="/pricing"
              data-testid="topbar-tier-badge"
              className={`hidden md:inline-flex items-center gap-1.5 text-[11px] font-mono uppercase tracking-widest border rounded-full px-3 py-1.5 transition ${
                tier.tier === "team"
                  ? "border-blue-500/40 bg-blue-500/10 text-blue-200 hover:bg-blue-500/15"
                  : tier.tier === "pro"
                  ? "border-violet-500/40 bg-violet-500/10 text-violet-200 hover:bg-violet-500/15"
                  : "border-white/10 bg-white/5 text-slate-300 hover:bg-white/10"
              }`}
            >
              {tier.tier === "starter" ? (
                <>
                  <Sparkle size={12} />
                  Free · Upgrade
                </>
              ) : (
                <>
                  <Crown size={12} />
                  {tier.tier_meta?.name}
                </>
              )}
            </NavLink>
          )}
          <div className="text-right hidden md:block">
            <div className="text-sm text-white" data-testid="topbar-user-name">
              {user?.name}
            </div>
            <div className="text-[11px] text-slate-500 font-mono">{user?.role?.toUpperCase()}</div>
          </div>
        </header>
        <div className="flex-1 px-4 sm:px-6 lg:px-8 py-6 sm:py-8 overflow-x-hidden">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
