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

// --- Raw Tapis token, for panels that call Tapis DIRECTLY from the browser ---
//
// Most Tapis access goes through the backend (/api/tapis-files/*), where every
// request re-reads the token cookie and the server resolves/refreshes the
// stored token per call — nothing can go stale. But tapis-file-explorer and
// ModelSelector talk to Tapis from client JS, so they need the raw JWT, and
// that copy is the app's ONLY long-lived one.
//
// It used to be held in each consumer's component state, fetched once on mount
// and never revisited. An open page therefore kept presenting whatever token it
// happened to load with — and a Tapis session re-authenticated behind that page
// (the user leaves, comes back, TapisUI has minted a new token) left the panel
// handing a dead JWT straight to Tapis, with no 401 for our own apiFetch to
// notice because the request never touched our backend.
//
// So: ONE cache for the whole app, keyed on the server-reported expiry, and
// invalidated whenever the tab is re-focused (below) — the exact moment a
// re-auth may have happened while we weren't looking.
let cachedTapisToken: { token: string; expiresAt: number } | null = null;

// Re-fetch this far before the expiry rather than at it, so a token that is
// about to lapse isn't handed to a caller who's about to use it. Mirrors the
// backend's own _EXPIRY_SKEW_SECONDS.
const TOKEN_EXPIRY_SKEW_MS = 60_000;

/** Drop the cached token so the next getTapisToken() goes back to the server. */
export function invalidateTapisToken(): void {
  cachedTapisToken = null;
}

/**
 * The current user's raw Tapis access token, or null when there isn't a usable
 * Tapis session (logged into the app but no Tapis credential — callers surface
 * that inline rather than bouncing the whole app to login).
 *
 * Served from cache only while the server-reported expiry is comfortably away;
 * otherwise re-fetched. Pass `force` to bypass the cache outright.
 */
export async function getTapisToken(force = false): Promise<string | null> {
  if (
    !force &&
    cachedTapisToken &&
    cachedTapisToken.expiresAt - Date.now() > TOKEN_EXPIRY_SKEW_MS
  ) {
    return cachedTapisToken.token;
  }

  const res = await apiFetch("/api/tapis/token");
  if (!res.ok) {
    cachedTapisToken = null;
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Could not fetch Tapis token (HTTP ${res.status}).`);
  }

  const { token, expires_at } = await res.json();
  if (!token) {
    cachedTapisToken = null;
    return null;
  }
  // A token with no readable `exp` is cached for one skew window rather than
  // indefinitely: unknown expiry should mean "check back soon", not "trust
  // forever" — the failure mode this whole accessor exists to prevent.
  const expiresAt = expires_at ? new Date(expires_at).getTime() : Date.now() + TOKEN_EXPIRY_SKEW_MS;
  cachedTapisToken = { token, expiresAt: Number.isNaN(expiresAt) ? 0 : expiresAt };
  return token;
}

// Returning to the tab is when a Tapis re-auth is most likely to have happened
// out from under us, so treat it as a cache barrier. Cheap: it costs one
// request, and only on the next actual use of the token.
//
// Registered at module scope, guarded for the SSR pass where there's no window.
// visibilitychange is listened for on `document` (its spec'd target) rather than
// on window: it does reach window by bubbling, but a listener there fires AFTER
// any document-level listener, so a consumer re-reading the token on the same
// event would have raced ahead of this invalidation and been served the stale
// cache. Consumers that re-resolve on visibility pass force anyway, but there's
// no reason to leave the ordering trap in place.
if (typeof window !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") invalidateTapisToken();
  });
  window.addEventListener("focus", invalidateTapisToken);
}
