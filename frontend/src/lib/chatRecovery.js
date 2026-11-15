// Pending chat submissions, held in sessionStorage so an interrupted request can be
// reconciled with the server instead of asking the user to retype their message.
//
// Entries are namespaced by authenticated user id, and every read re-checks that the entry
// belongs to the account asking for it. A message saved by one account is never replayed
// under another, and nothing outlives a logout. sessionStorage (not localStorage) is
// deliberate: this is user content, and a reload -- not a new browser session -- is the case
// worth surviving.

const PREFIX = "dl.chat.pending.";

function store() {
  try {
    return window.sessionStorage;
  } catch {
    // Blocked storage (private mode, hardened settings) must not break chatting.
    return null;
  }
}

function keysWithPrefix(s) {
  const keys = [];
  for (let i = 0; i < s.length; i += 1) {
    const key = s.key(i);
    if (key && key.startsWith(PREFIX)) keys.push(key);
  }
  return keys;
}

export function savePending(userId, entry) {
  if (!userId) return;
  const s = store();
  if (!s) return;
  try {
    s.setItem(PREFIX + userId, JSON.stringify({ ...entry, userId }));
  } catch {
    // Quota or serialisation failure: recovery is an optimisation, never a blocker.
  }
}

export function readPending(userId) {
  if (!userId) return null;
  const s = store();
  if (!s) return null;
  const raw = s.getItem(PREFIX + userId);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    // A copied or tampered entry must never be submitted under the wrong account.
    if (!parsed || parsed.userId !== userId || !parsed.requestId || !parsed.message) {
      clearPending(userId);
      return null;
    }
    return parsed;
  } catch {
    clearPending(userId);
    return null;
  }
}

export function clearPending(userId) {
  const s = store();
  if (!s) return;
  try {
    s.removeItem(PREFIX + userId);
  } catch {
    // ignore
  }
}

export function clearAllPending() {
  const s = store();
  if (!s) return;
  try {
    keysWithPrefix(s).forEach((key) => s.removeItem(key));
  } catch {
    // ignore
  }
}

export function prunePendingForOtherUsers(userId) {
  const s = store();
  if (!s) return;
  const mine = PREFIX + userId;
  try {
    keysWithPrefix(s)
      .filter((key) => key !== mine)
      .forEach((key) => s.removeItem(key));
  } catch {
    // ignore
  }
}

export function newRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  // randomUUID requires a secure context; a plain-HTTP deployment would not have it.
  // getRandomValues does not, so fall back to a v4-shaped id rather than throwing.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
