import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

const api = axios.create({
  baseURL: API,
  withCredentials: true,
});

export function formatApiError(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e)))
      .filter(Boolean)
      .join(" ");
  if (detail && typeof detail.message === "string") return detail.message;
  if (detail && typeof detail.msg === "string") return detail.msg;
  if (detail && typeof detail.detail === "string") return detail.detail;
  return String(detail);
}

// A 402 from tier enforcement carries structured quota data next to the message, so the
// UI can show what was hit, how much was used, and where to upgrade. Returns null for
// every other failure, so callers can fall back to formatApiError.
export function limitInfo(err) {
  const detail = err && err.response && err.response.data && err.response.data.detail;
  if (!detail || typeof detail !== "object" || detail.code !== "limit_exceeded") return null;
  return detail;
}

export function isLimitError(err) {
  return limitInfo(err) != null;
}

export default api;
