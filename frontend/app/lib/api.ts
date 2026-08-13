// Central helper for calling the backend. Sends the session cookie with every
// request (credentials: "include") so the Tapis-OAuth session is honored, and
// bounces the browser to the login flow on a 401.
//
// All callers run client-side (clientLoader / event handlers), so the browser
// attaches the cross-origin session cookie under the backend's CORS
// allow_credentials policy.
//
// Embedded in TapisUI (see lib/embed.ts), the host's X-Tapis-Token cookie is the
// credential instead — forwarded as a header for the cross-site case — and a 401
// must NOT redirect: there is no login for this app to run, and the redirect
// would only blank out the host's iframe.

import { TAPIS_TOKEN_COOKIE, hostOwnsAuth, tapisTokenFromCookie } from "./embed";

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
  // Never in embedded mode: the host authenticated the user already, and Tapis
  // refuses to render its login page inside a frame.
  if (typeof window !== "undefined" && !hostOwnsAuth()) {
    window.location.href = loginUrl();
  }
}

// Auth headers to add to a backend call: the host's Tapis token when we can read
// it, nothing otherwise (the session cookie travels on its own).
function authHeaders(): Record<string, string> {
  const token = tapisTokenFromCookie();
  return token ? { [TAPIS_TOKEN_COOKIE]: token } : {};
}

export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  // Callers pass a path ("/api/..."); prepend the configured backend base URL.
  // Absolute URLs are left as-is for backward compatibility.
  const url = input.startsWith("/") ? `${BACKEND_URL}${input}` : input;
  // Headers(), not an object spread: callers pass plain objects today, but a
  // Headers instance or entry array would spread to {} and silently lose the
  // caller's own headers.
  const headers = new Headers(init?.headers);
  const token = tapisTokenFromCookie();
  if (token) headers.set(TAPIS_TOKEN_COOKIE, token);

  const res = await fetch(url, { ...init, credentials: "include", headers });
  if (res.status === 401 && !hostOwnsAuth()) {
    redirectToLogin();
    // Stop callers from proceeding to parse a 401 body while the redirect happens.
    await new Promise(() => {});
  }
  return res;
}

export async function logout() {
  window.location.href = `${BACKEND_URL}/logout`;
}

export type CurrentUser = {
  username: string;
  // "tapis-token" when the embedding host supplied the credential — the UI hides
  // its own login/logout in that mode.
  auth_mode?: "tapis-token" | "session";
};

export async function fetchCurrentUser(): Promise<CurrentUser | null> {
  try {
    const res = await fetch(`${BACKEND_URL}/me`, {
      credentials: "include",
      headers: authHeaders(),
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}
