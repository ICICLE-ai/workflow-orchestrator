// Central helper for calling the backend. Sends the session cookie with every
// request (credentials: "include") so the Tapis-OAuth session is honored, and
// bounces the browser to the login flow on a 401.
//
// All callers run client-side (clientLoader / event handlers), so the browser
// attaches the cross-origin session cookie under the backend's CORS
// allow_credentials policy.

// Backend base URL. Set VITE_BACKEND_URL in frontend/.env to point the app at a
// non-local backend (e.g. a cloudflared tunnel URL); defaults to the local dev
// backend. Note: when the frontend and backend are on different hosts, the
// backend's session cookie must be SameSite=None; Secure (see backend .env) for
// the browser to send it on these cross-site API calls.
// Trailing slashes are stripped so callers' paths ("/api/...") never produce a
// double slash (e.g. ".../com//api/..."), which the backend would 404.
export const BACKEND_URL: string = (
  import.meta.env.VITE_BACKEND_URL ?? "http://localhost:8002"
).replace(/\/+$/, "");

// SAM3-assisted annotation service base URL (Smart Labeler's magic-wand tool
// POSTs to `${SAM3_ENDPOINT}/predict`) — a fixed deployment detail, not a
// per-node setting. Set VITE_SAM3_ENDPOINT in frontend/.env; leave blank to
// disable the tool.
export const SAM3_ENDPOINT: string = (import.meta.env.VITE_SAM3_ENDPOINT ?? "").replace(/\/+$/, "");

export function loginUrl() {
  return `${BACKEND_URL}/login`;
}

export function redirectToLogin() {
  if (typeof window !== "undefined") {
    window.location.href = loginUrl();
  }
}

export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  // Callers pass a path ("/api/..."); prepend the configured backend base URL.
  // Absolute URLs are left as-is for backward compatibility.
  const url = input.startsWith("/") ? `${BACKEND_URL}${input}` : input;
  const res = await fetch(url, { ...init, credentials: "include" });
  if (res.status === 401) {
    redirectToLogin();
    // Stop callers from proceeding to parse a 401 body while the redirect happens.
    await new Promise(() => {});
  }
  return res;
}

export async function logout() {
  window.location.href = `${BACKEND_URL}/logout`;
}

export async function fetchCurrentUser(): Promise<{ username: string } | null> {
  try {
    const res = await fetch(`${BACKEND_URL}/me`, { credentials: "include" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}
