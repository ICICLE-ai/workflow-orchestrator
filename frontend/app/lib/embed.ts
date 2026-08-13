// Embedded mode: this app running inside TapisUI's iframe.
//
// TapisUI authenticates the user itself and passes the Tapis token down as the
// `X-Tapis-Token` cookie. When that cookie is there, the user IS already
// authenticated — the app must not start its own OAuth flow, and must not offer
// login/logout controls that would fight the host for the session. Redirecting
// to Tapis' authorize page from inside an iframe wouldn't work anyway: Tapis
// sends frame-ancestors headers that stop it rendering there, so the user would
// just see an empty frame.
//
// The cookie normally reaches the backend on its own (same parent domain). When
// the API is on a different site, the browser won't attach it to those requests,
// so api.ts forwards the value it can read here as a header of the same name.
// That readback requires the cookie NOT be HttpOnly; if the host marks it
// HttpOnly, deploy the API under the same parent domain and the direct cookie
// path covers it.

// The header name we forward a token in, and the cookie names a host may have
// left it under. TapisUI's own cookie is `tapis-token`, and its value is NOT a
// bare JWT — it's a percent-encoded JSON envelope written by a JS cookie
// library: {"access_token":"eyJ...","expires_in":14400}. Both shapes are read.
export const TAPIS_TOKEN_COOKIE = "X-Tapis-Token";
export const TAPISUI_TOKEN_COOKIE = "tapis-token";

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null; // SSR pass
  for (const part of document.cookie.split(";")) {
    const eq = part.indexOf("=");
    if (eq === -1) continue;
    if (part.slice(0, eq).trim() !== name) continue;
    const raw = part.slice(eq + 1).trim().replace(/^"|"$/g, "");
    try {
      return decodeURIComponent(raw) || null;
    } catch {
      return raw || null;
    }
  }
  return null;
}

/** Pull a JWT out of a cookie value that may be bare or a JSON envelope. */
function extractJwt(raw: string | null): string | null {
  const value = raw?.trim().replace(/^"|"$/g, "").trim();
  if (!value) return null;
  if (!value.startsWith("{")) {
    return value.split(".").length === 3 ? value : null; // bare JWT
  }
  try {
    const token = JSON.parse(value)?.access_token;
    const jwt = typeof token === "object" ? token?.access_token : token;
    return typeof jwt === "string" && jwt.split(".").length === 3 ? jwt : null;
  } catch {
    return null;
  }
}

/** The host-supplied Tapis token, if this browser has one we can read. */
export function tapisTokenFromCookie(): string | null {
  return (
    extractJwt(readCookie(TAPIS_TOKEN_COOKIE)) ??
    extractJwt(readCookie(TAPISUI_TOKEN_COOKIE))
  );
}

/** True when the page is rendered inside another site's frame. */
export function isFramed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.self !== window.top;
  } catch {
    // Cross-origin parent — the access threw, which itself proves we're framed.
    return true;
  }
}

/**
 * True when authentication is the host's job, not ours: either we can see the
 * host's token cookie, or we're in someone's iframe (where the cookie may be
 * present but unreadable — HttpOnly, or scoped so only the backend sees it).
 * Both cases mean: never bounce this window to the Tapis login flow.
 */
export function hostOwnsAuth(): boolean {
  return tapisTokenFromCookie() !== null || isFramed();
}
