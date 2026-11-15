import React, { createContext, useContext, useEffect, useState } from "react";
import api, { formatApiError } from "@/lib/api";
import { clearAllPending, prunePendingForOtherUsers } from "@/lib/chatRecovery";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null); // null=loading initial
  const [ready, setReady] = useState(false);

  useEffect(() => {
    api
      .get("/auth/me")
      .then((r) => setUser(r.data))
      .catch(() => setUser(false))
      .finally(() => setReady(true));
  }, []);

  async function login(email, password) {
    try {
      const r = await api.post("/auth/login", { email, password });
      // A pending chat submission belongs to the account that created it. Signing in on a
      // shared tab must not leave the previous account's message queued for replay.
      prunePendingForOtherUsers(r.data?.id);
      setUser(r.data);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: formatApiError(e.response?.data?.detail) || e.message };
    }
  }

  async function register(email, password, name) {
    try {
      const r = await api.post("/auth/register", { email, password, name });
      prunePendingForOtherUsers(r.data?.id);
      setUser(r.data);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: formatApiError(e.response?.data?.detail) || e.message };
    }
  }

  async function updateProfile(name) {
    try {
      const r = await api.patch("/auth/me", { name });
      setUser(r.data);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: formatApiError(e.response?.data?.detail) || e.message };
    }
  }

  async function changePassword(currentPassword, newPassword) {
    try {
      await api.post("/auth/change-password", {
        current_password: currentPassword,
        new_password: newPassword,
      });
      return { ok: true };
    } catch (e) {
      return { ok: false, error: formatApiError(e.response?.data?.detail) || e.message };
    }
  }

  async function logout() {
    try {
      await api.post("/auth/logout");
    } catch (e) {
      // ignore - logout is best-effort
    }
    // Nothing may outlive the session: a queued chat submission is user content, and it
    // must not be recoverable by whoever signs in next on this tab.
    clearAllPending();
    setUser(false);
  }

  return (
    <AuthContext.Provider
      value={{ user, ready, login, register, logout, updateProfile, changePassword }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  return useContext(AuthContext);
}
