"""Authentication: an inbound Tapis token (embedded mode) or Tapis OAuth2 login.

Two ways in, checked in this order on every request:

1. **A host-supplied Tapis token** — when this app is iframed inside TapisUI, the
   host already holds the user's token and leaves it in a cookie (`tapis-token`,
   or `X-Tapis-Token`; see the constants below for the shapes). If a valid one is
   present the user is already authenticated: we verify it, upsert the AppUser,
   store the token so the engine submits jobs as them, and NEVER start an OAuth
   redirect. Bouncing to Tapis' authorize page from inside an iframe would fail
   on frame-ancestors anyway — the point of this mode is that there is nothing
   left to authenticate.

2. **Session cookie** — the standalone flow. The browser hits /login, we redirect
   to Tapis' authorize endpoint, Tapis redirects back to /oauth2/callback with a
   code, we exchange it (confidential client, see engine.tapis_auth) for an
   access + refresh token, upsert the AppUser, persist the tokens, and set a
   signed session cookie (SessionMiddleware, configured in main.py). The engine
   later resolves each run owner's token from the DB.

Local dev without a registered Tapis client: set TAPIS_USE_MOCK=true and /login
short-circuits to a mock user session so the app stays runnable.

resolve_current_user() is the single entry point for both; main, geospatial and
annotation_adapter all authenticate through it.
"""
import hashlib
import json
import os
import secrets
import time
from urllib.parse import unquote, urlencode

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from db import get_db
from models import AppUser, Team
from engine import tapis_auth

router = APIRouter()

# Where an inbound Tapis token can come from, in priority order.
#
# TapisUI's own cookie is `tapis-token`, and its value is not a bare JWT — it's a
# percent-encoded JSON envelope written by the JS cookie library:
#
#   tapis-token={%22access_token%22:%22eyJhbGci...%22%2C%22expires_in%22:14400}
#
# so the value has to be unquoted and parsed to get at `access_token`. The
# X-Tapis-Token names are kept alongside it: the header is what the frontend
# forwards when the backend is on a different site than the cookie's domain, and
# a plain `X-Tapis-Token` cookie is what a host that hands over a bare JWT would
# set. All three are accepted; whichever turns up first wins.
TAPIS_TOKEN_COOKIE = "X-Tapis-Token"
TAPISUI_TOKEN_COOKIE = "tapis-token"

TAPIS_BASE_URL = os.getenv("TAPIS_BASE_URL", "https://icicleai.tapis.io").rstrip("/")
CLIENT_ID = os.getenv("TAPIS_CLIENT_ID")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8002").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

REDIRECT_URI = f"{APP_BASE_URL}/oauth2/callback"


def _use_mock() -> bool:
    return os.getenv("TAPIS_USE_MOCK", "false").lower() == "true"


def _upsert_user(db: Session, username: str, email: str | None = None) -> AppUser:
    """Find or create an AppUser by username, attaching them to the default team."""
    user = db.query(AppUser).filter(AppUser.username == username).first()
    if user:
        return user
    team = db.query(Team).filter(Team.name == "default_team").first()
    if not team:
        team = Team(name="default_team", description="Default Team")
        db.add(team)
        db.commit()
        db.refresh(team)
    user = AppUser(username=username, email=email, team_id=team.team_id)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# Verifying an inbound token means an RSA signature check (or, in the fallback
# path, a round trip to Tapis) — too much to repeat on every request of a page
# that fires a dozen. Cache the verified username per token for a short window,
# keyed by digest so raw tokens aren't held in a long-lived structure. Entries
# are also bounded by the token's own expiry, so a cached entry can never outlive
# the token it stands for.
_VERIFY_CACHE_TTL_SECONDS = 300
_verified_tokens: dict[str, tuple[str, float]] = {}


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _verified_username(token: str) -> str | None:
    """Verify an inbound Tapis token and return its username, or None if the
    token is invalid/expired. Memoized for _VERIFY_CACHE_TTL_SECONDS."""
    digest = _token_digest(token)
    now = time.monotonic()
    cached = _verified_tokens.get(digest)
    if cached and cached[1] > now:
        return cached[0]

    claims = tapis_auth.verify_access_token(token)
    username = (claims or {}).get("tapis/username") or (claims or {}).get("sub")
    if not username:
        _verified_tokens.pop(digest, None)
        return None

    # Don't cache past the token's own expiry.
    ttl = _VERIFY_CACHE_TTL_SECONDS
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        ttl = min(ttl, max(0.0, float(exp) - time.time()))
    if len(_verified_tokens) > 512:  # bounded: drop whatever has lapsed
        for key, (_, expires) in list(_verified_tokens.items()):
            if expires <= now:
                del _verified_tokens[key]
    _verified_tokens[digest] = (username, now + ttl)
    return username


def extract_jwt(raw: str | None) -> str | None:
    """Pull a JWT out of a cookie/header value, which may be either a bare token
    or TapisUI's percent-encoded `{"access_token": "...", "expires_in": ...}`
    envelope. Returns None if there's no usable token in there.

    Starlette's cookie parser only undoes SimpleCookie's octal quoting, not the
    percent-encoding a JS cookie library applies, so the unquote here is load
    bearing — without it the value is `{%22access_token%22:...}` and json.loads
    rejects it.
    """
    if not raw:
        return None
    value = unquote(raw.strip()).strip('"').strip()
    if not value:
        return None
    if not value.startswith("{"):
        # Bare JWT: three base64url segments.
        return value if value.count(".") == 2 else None
    try:
        payload = json.loads(value)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("access_token")
    if isinstance(token, dict):  # Tapis' nested {access_token: {access_token: ...}}
        token = token.get("access_token")
    return token if isinstance(token, str) and token.count(".") == 2 else None


def _all_cookie_values(request: Request, name: str) -> list[str]:
    """Every value the request carries for cookie `name`, in the order the
    browser sent them.

    request.cookies is a dict, so it collapses duplicates and keeps only the
    LAST — but a browser legitimately sends the same cookie name more than once
    when copies exist on different domains/paths (e.g. a leftover on `.tapis.io`
    plus a fresh one on the tenant host, which is what a logout/re-login
    produces when the new cookie doesn't exactly replace the old). RFC 6265 has
    the browser send the more-specific one FIRST, so the dict parse discards the
    newer value and keeps the stale one — precisely backwards. We re-read the
    raw header to see them all and let verification pick.
    """
    header = request.headers.get("cookie")
    if not header:
        return []
    values = []
    for pair in header.split(";"):
        key, sep, value = pair.strip().partition("=")  # JWTs/base64 contain '='
        if sep and key.strip() == name:
            values.append(value.strip())
    return values


def tapis_token_candidates(request: Request) -> list[str]:
    """Every distinct inbound Tapis token this request carries, in precedence
    order (see the constants above).

    Deliberately a list, not a single value. A long-lived browser profile
    accumulates cookies: an `X-Tapis-Token` left behind by an earlier session
    can sit alongside the `tapis-token` TapisUI just wrote, and BOTH parse as
    JWTs — extract_jwt only checks shape, never signature or expiry. Returning
    just the highest-precedence one would let the stale cookie mask the live
    one, which is why embedded auth fails in a normal profile but works in a
    fresh/incognito one where no leftovers exist.

    Callers try these in order and take the first that VERIFIES, so precedence
    still decides between two equally-valid tokens while a dead cookie can no
    longer shadow a good one. Precedence is unchanged from before: the
    production `X-Tapis-Token` cookie, then the same name as a header, then
    localhost TapisUI's `tapis-token`.

    Duplicates of a single cookie name are included too (see _all_cookie_values)
    — a re-login can leave two `X-Tapis-Token`s in flight, and the stale one is
    not necessarily the one request.cookies surfaces.
    """
    raw_values: list[str | None] = [
        *_all_cookie_values(request, TAPIS_TOKEN_COOKIE),
        request.cookies.get(TAPIS_TOKEN_COOKIE),
        request.headers.get(TAPIS_TOKEN_COOKIE),
        *_all_cookie_values(request, TAPISUI_TOKEN_COOKIE),
        request.cookies.get(TAPISUI_TOKEN_COOKIE),
    ]
    tokens: list[str] = []
    for raw in raw_values:
        token = extract_jwt(raw)
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def tapis_token_from_request(request: Request) -> str | None:
    """The inbound Tapis token to act on: the first candidate that verifies,
    else the first that merely parses (so diagnostics can still report on a
    token that arrived but was rejected), else None."""
    tokens = tapis_token_candidates(request)
    for token in tokens:
        if _verified_username(token):
            return token
    return tokens[0] if tokens else None


def _sync_inbound_token(user: AppUser, token: str, db: Session) -> None:
    """Persist the host-supplied token on the user so the DBOS engine submits
    that user's jobs with it (get_token_for_run reads it from the DB, long after
    the request is gone). Written only when it actually changed — this runs on
    every authenticated request."""
    if user.tapis_access_token == token:
        return
    user.tapis_access_token = token
    user.tapis_token_expires_at = tapis_auth.token_expiry(token)
    db.commit()


def resolve_current_user_with_mode(request: Request, db: Session) -> tuple[AppUser | None, str | None]:
    """Authenticate a request. Returns (user, mode) — mode is "tapis-token" or
    "session" — or (None, None) if no credential holds up.

    A valid X-Tapis-Token wins over the session cookie, so an embedded app always
    acts as the user the host says is there rather than whoever last logged in
    standalone in this browser.

    EVERY inbound token is tried, not just the highest-precedence one: a stale
    leftover cookie must not shadow the live token sitting behind it (see
    tapis_token_candidates).

    A token that FAILS verification falls through to the next candidate, and
    finally to the session, instead of hard-failing: it asserted no identity we
    can act on, and rejecting outright would strand anyone whose browser holds a
    lapsed TapisUI cookie from a shared parent domain — every request 401s, and
    logging in can't help because the dead cookie keeps winning.
    """
    for token in tapis_token_candidates(request):
        username = _verified_username(token)
        if username:
            user = _upsert_user(db, username)
            _sync_inbound_token(user, token, db)
            return user, "tapis-token"
        print("[auth] an inbound Tapis token failed verification; "
              "trying the next credential")

    username = request.session.get("username")
    if not username:
        return None, None
    user = db.query(AppUser).filter(AppUser.username == username).first()
    return (user, "session") if user else (None, None)


def resolve_current_user(request: Request, db: Session) -> AppUser | None:
    """Authenticate a request, or return None. See resolve_current_user_with_mode."""
    return resolve_current_user_with_mode(request, db)[0]


def require_current_user(request: Request, db: Session = Depends(get_db)) -> AppUser:
    """FastAPI dependency: the signed-in AppUser, or 401. The single auth entry
    point for every protected route in the app."""
    user = resolve_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.get("/login")
def login(request: Request, db: Session = Depends(get_db)):
    """Start the Tapis OAuth2 authorization-code flow.

    Short-circuits when the request already carries a valid X-Tapis-Token (the
    embedded/TapisUI case): there is nothing to authenticate, so bounce straight
    back to the frontend instead of redirecting to Tapis — which would break in
    an iframe regardless.

    In mock mode, skip Tapis entirely: sign in a local mock user and bounce back
    to the frontend so the app is usable without a registered client.
    """
    user, mode = resolve_current_user_with_mode(request, db)
    if user and mode == "tapis-token":
        return RedirectResponse(url=FRONTEND_URL)

    if _use_mock():
        user = _upsert_user(db, "mock_user", "mock@example.com")
        request.session["username"] = user.username
        return RedirectResponse(url=FRONTEND_URL)

    if not tapis_auth.oauth_client_configured():
        raise HTTPException(
            status_code=500,
            detail="Tapis OAuth client not configured (set TAPIS_CLIENT_ID/TAPIS_CLIENT_KEY, "
                   "or TAPIS_USE_MOCK=true for local development).",
        )

    # CSRF: remember a random state and require it back on the callback.
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    query = urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })
    return RedirectResponse(url=f"{TAPIS_BASE_URL}/v3/oauth2/authorize?{query}")


@router.get("/oauth2/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None):
    """Handle the redirect back from Tapis: verify state, exchange the code for
    tokens, upsert the user, persist tokens, and set the session cookie."""
    if error:
        raise HTTPException(status_code=400, detail=f"Tapis authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state (possible CSRF)")

    tokens = tapis_auth.exchange_code_for_tokens(code, REDIRECT_URI)
    if not tokens:
        raise HTTPException(status_code=502, detail="Failed to exchange code for a Tapis token")

    username = tapis_auth.username_from_jwt(tokens["access_token"])
    if not username:
        raise HTTPException(status_code=502, detail="Could not determine username from Tapis token")

    db = next(get_db())
    try:
        user = _upsert_user(db, username)
        tapis_auth.store_tokens(user, tokens, db)
        request.session["username"] = user.username
    finally:
        db.close()

    return RedirectResponse(url=FRONTEND_URL)


@router.get("/auth-debug")
def auth_debug(request: Request):
    """Report what credential this request actually carried, and why it was or
    wasn't accepted. Open it from inside the iframe (or curl it) when embedded
    auth 401s and you can't tell whether the token reached the server at all —
    "the cookie exists in the browser" and "the cookie reached this backend" are
    very different things once more than one domain is involved.

    Leaks nothing: no token, no secret, and the only identity it reports is the
    one the caller themselves supplied.
    """
    from jose import jwt as _jwt

    sources = {
        f"{TAPIS_TOKEN_COOKIE} cookie": request.cookies.get(TAPIS_TOKEN_COOKIE),
        f"{TAPIS_TOKEN_COOKIE} header": request.headers.get(TAPIS_TOKEN_COOKIE),
        f"{TAPISUI_TOKEN_COOKIE} cookie": request.cookies.get(TAPISUI_TOKEN_COOKIE),
    }
    known = {TAPIS_TOKEN_COOKIE, TAPISUI_TOKEN_COOKIE}

    info = {
        "request_origin": request.headers.get("origin"),
        # Present at all vs. actually yielded a JWT — a host cookie whose shape
        # we can't parse shows up as present-but-unusable rather than absent.
        "sources_present": [name for name, raw in sources.items() if raw],
        "sources_yielding_a_jwt": [name for name, raw in sources.items() if extract_jwt(raw)],
        # The one that matters when more than one source carries a JWT: a source
        # that parses but does NOT verify is a stale leftover. If a non-verifying
        # source is listed above a verifying one, that leftover used to win and
        # 401 the request — the exact "works in incognito, fails normally" bug.
        "sources_with_a_verifying_jwt": [
            name for name, raw in sources.items()
            if (tok := extract_jwt(raw)) and _verified_username(tok)
        ],
        "other_cookies_seen": sorted(k for k in request.cookies if k not in known),
        "session_username": request.session.get("username"),
        "backend_expects_tenant": tapis_auth.TAPIS_TENANT,
        "tenant_public_key_available": tapis_auth.tenant_public_key() is not None,
    }

    token = tapis_token_from_request(request)
    if not token:
        info["verdict"] = (
            "a token-shaped cookie/header arrived but no JWT could be read out of it"
            if info["sources_present"]
            else "no Tapis token reached this backend at all (no cookie, no header)"
        )
        return info

    try:
        claims = _jwt.get_unverified_claims(token)
    except Exception as e:
        info["verdict"] = f"value is not a readable JWT ({type(e).__name__})"
        return info

    exp = claims.get("exp")
    info["token_claims"] = {
        "username": claims.get("tapis/username"),
        "tenant_id": claims.get("tapis/tenant_id"),
        "token_type": claims.get("tapis/token_type"),
        "expires_in_seconds": None if exp is None else int(float(exp) - time.time()),
    }
    info["verified"] = tapis_auth.verify_access_token(token) is not None
    info["verdict"] = (
        "accepted" if info["verified"]
        else "token reached the backend but failed verification — compare tenant_id "
             "against backend_expects_tenant, and check expires_in_seconds"
    )
    return info


@router.get("/logout")
def logout(request: Request):
    """Clear the session and return to the frontend.

    Only this app's own session is cleared. In embedded mode the identity comes
    from the host's X-Tapis-Token cookie, which belongs to TapisUI — signing out
    of the host is the host's business, not ours (and the frontend hides the
    logout control there for exactly that reason).
    """
    request.session.clear()
    return RedirectResponse(url=FRONTEND_URL)


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    """Report the currently signed-in user (used by the frontend to show login
    state). 401 when the request carries no usable credential.

    `auth_mode` tells the frontend which world it's in: in "tapis-token" mode the
    host owns the session, so the UI must not offer login/logout or bounce to the
    OAuth flow on a 401.
    """
    user, mode = resolve_current_user_with_mode(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": user.username, "auth_mode": mode}
