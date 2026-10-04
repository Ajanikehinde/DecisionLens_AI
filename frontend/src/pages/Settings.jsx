import React, { useEffect, useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { User, Bell, Lock, Check, AlertTriangle } from "lucide-react";

// The API Keys panel was removed: it rendered a hardcoded placeholder key next to a
// button that did nothing, which read as a working feature. Notification toggles are
// likewise local-only until a preferences endpoint exists, and are labelled as such.
const TABS = [
  { key: "profile", label: "Profile", icon: User },
  { key: "notifications", label: "Notifications", icon: Bell },
  { key: "security", label: "Security", icon: Lock },
];

const NOTIFICATIONS = [
  "Decision deadline reminders",
  "Weekly digest",
  "AI recommendation alerts",
  "Stakeholder mentions",
];

export default function Settings() {
  const { user, updateProfile, changePassword } = useAuth();
  const [tab, setTab] = useState("profile");

  const [name, setName] = useState(user?.name || "");
  const [savingProfile, setSavingProfile] = useState(false);
  const [profileMsg, setProfileMsg] = useState(null);

  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [savingPw, setSavingPw] = useState(false);
  const [pwMsg, setPwMsg] = useState(null);

  const [prefs, setPrefs] = useState({});

  // Keep the field in sync when the auth context finishes loading or refreshes.
  useEffect(() => {
    if (user?.name) setName(user.name);
  }, [user?.name]);

  async function onSaveProfile(e) {
    e.preventDefault();
    setProfileMsg(null);
    const trimmed = name.trim();
    if (!trimmed) {
      setProfileMsg({ kind: "error", text: "Name cannot be empty." });
      return;
    }
    if (trimmed === user?.name) {
      setProfileMsg({ kind: "ok", text: "No changes to save." });
      return;
    }
    setSavingProfile(true);
    const r = await updateProfile(trimmed);
    setSavingProfile(false);
    setProfileMsg(
      r.ok
        ? { kind: "ok", text: "Profile updated." }
        : { kind: "error", text: r.error }
    );
  }

  async function onChangePassword(e) {
    e.preventDefault();
    setPwMsg(null);
    if (newPw.length < 8) {
      setPwMsg({ kind: "error", text: "New password must be at least 8 characters." });
      return;
    }
    if (newPw !== confirmPw) {
      setPwMsg({ kind: "error", text: "New passwords do not match." });
      return;
    }
    if (newPw === currentPw) {
      setPwMsg({ kind: "error", text: "New password must differ from the current one." });
      return;
    }
    setSavingPw(true);
    const r = await changePassword(currentPw, newPw);
    setSavingPw(false);
    if (r.ok) {
      setCurrentPw("");
      setNewPw("");
      setConfirmPw("");
      setPwMsg({ kind: "ok", text: "Password changed." });
    } else {
      setPwMsg({ kind: "error", text: r.error });
    }
  }

  function StatusMessage({ msg }) {
    if (!msg) return null;
    const Icon = msg.kind === "ok" ? Check : AlertTriangle;
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid={`settings-${msg.kind}-message`}
        className={`text-sm flex items-center gap-2 ${
          msg.kind === "ok" ? "text-emerald-400" : "text-rose-400"
        }`}
      >
        <Icon size={14} />
        {msg.text}
      </p>
    );
  }

  return (
    <div className="space-y-6" data-testid="settings-page">
      <div>
        <div className="label-eyebrow">Account</div>
        <h1 className="font-sora text-3xl font-semibold mt-1">Settings</h1>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[200px,1fr] gap-6">
        <div className="space-y-1" role="tablist" aria-label="Settings sections">
          {TABS.map((t) => {
            const Icon = t.icon;
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                data-testid={`settings-tab-${t.key}`}
                role="tab"
                aria-selected={active}
                aria-controls={`settings-panel-${t.key}`}
                id={`settings-tabbtn-${t.key}`}
                className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors ${
                  active
                    ? "bg-violet-600/15 text-violet-200 border border-violet-500/30"
                    : "text-slate-400 hover:text-slate-200 hover:bg-white/5 border border-transparent"
                }`}
              >
                <Icon size={14} />
                {t.label}
              </button>
            );
          })}
        </div>

        <div className="card p-6">
          {tab === "profile" && (
            <form
              onSubmit={onSaveProfile}
              className="space-y-4"
              data-testid="settings-profile"
              role="tabpanel"
              id="settings-panel-profile"
              aria-labelledby="settings-tabbtn-profile"
            >
              <h2 className="font-sora text-lg font-semibold">Profile</h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label
                    htmlFor="settings-name"
                    className="text-xs text-slate-400 mb-1 block"
                  >
                    Name
                  </label>
                  <input
                    id="settings-name"
                    name="name"
                    className="input"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    maxLength={80}
                    required
                  />
                </div>
                <div>
                  <label
                    htmlFor="settings-email"
                    className="text-xs text-slate-400 mb-1 block"
                  >
                    Email
                  </label>
                  <input
                    id="settings-email"
                    name="email"
                    className="input"
                    value={user?.email || ""}
                    disabled
                  />
                  <p className="text-[11px] text-slate-500 mt-1">
                    Email cannot be changed.
                  </p>
                </div>
              </div>
              <StatusMessage msg={profileMsg} />
              <button
                type="submit"
                className="btn btn-primary mt-2 disabled:opacity-60"
                disabled={savingProfile}
                data-testid="settings-save-profile"
              >
                {savingProfile ? "Saving…" : "Save changes"}
              </button>
            </form>
          )}

          {tab === "notifications" && (
            <div
              className="space-y-3"
              data-testid="settings-notifications"
              role="tabpanel"
              id="settings-panel-notifications"
              aria-labelledby="settings-tabbtn-notifications"
            >
              <h2 className="font-sora text-lg font-semibold">Notifications</h2>
              <p className="text-sm text-slate-400">
                Preferences are stored in this browser only. Server-side delivery is
                not enabled yet.
              </p>
              {NOTIFICATIONS.map((n) => (
                <label
                  key={n}
                  className="flex items-center justify-between p-3 border border-white/5 rounded-lg cursor-pointer"
                >
                  <span className="text-sm text-slate-300">{n}</span>
                  <input
                    type="checkbox"
                    checked={Boolean(prefs[n])}
                    onChange={(e) => setPrefs((p) => ({ ...p, [n]: e.target.checked }))}
                    aria-label={n}
                    className="accent-violet-500 h-4 w-4"
                  />
                </label>
              ))}
            </div>
          )}

          {tab === "security" && (
            <form
              onSubmit={onChangePassword}
              className="space-y-4"
              data-testid="settings-security"
              role="tabpanel"
              id="settings-panel-security"
              aria-labelledby="settings-tabbtn-security"
            >
              <h2 className="font-sora text-lg font-semibold">Security</h2>
              <div>
                <label
                  htmlFor="settings-current-pw"
                  className="text-xs text-slate-400 mb-1 block"
                >
                  Current password
                </label>
                <input
                  id="settings-current-pw"
                  type="password"
                  autoComplete="current-password"
                  className="input"
                  value={currentPw}
                  onChange={(e) => setCurrentPw(e.target.value)}
                  required
                />
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label
                    htmlFor="settings-new-pw"
                    className="text-xs text-slate-400 mb-1 block"
                  >
                    New password
                  </label>
                  <input
                    id="settings-new-pw"
                    type="password"
                    autoComplete="new-password"
                    className="input"
                    value={newPw}
                    onChange={(e) => setNewPw(e.target.value)}
                    minLength={8}
                    required
                  />
                </div>
                <div>
                  <label
                    htmlFor="settings-confirm-pw"
                    className="text-xs text-slate-400 mb-1 block"
                  >
                    Confirm new password
                  </label>
                  <input
                    id="settings-confirm-pw"
                    type="password"
                    autoComplete="new-password"
                    className="input"
                    value={confirmPw}
                    onChange={(e) => setConfirmPw(e.target.value)}
                    minLength={8}
                    required
                  />
                </div>
              </div>
              <p className="text-[11px] text-slate-500">
                Minimum 8 characters. Your current session stays signed in.
              </p>
              <StatusMessage msg={pwMsg} />
              <button
                type="submit"
                className="btn btn-primary disabled:opacity-60"
                disabled={savingPw}
                data-testid="settings-change-password"
              >
                {savingPw ? "Updating…" : "Change password"}
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}