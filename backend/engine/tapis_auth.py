"""Tapis OAuth2 credential handling (authorization-code flow, per-user tokens).

The app authenticates users through Tapis' OAuth2 authorization-code flow using a
confidential client (TAPIS_CLIENT_ID + TAPIS_CLIENT_KEY). The login handler
(backend/auth.py) exchanges the returned `code` for an access + refresh token,
which are stored per-user on AppUser. The DBOS engine resolves the *run owner's*
token via get_token_for_run() so each job is submitted as the user who launched
it, refreshing transparently when the short-lived access token expires mid-run.

Secrets (tokens, client key) are never printed. `describe_credentials()` reports
only which client is configured, never any secret material.
"""
import os
from datetime import datetime, timedelta, timezone

import httpx
from jose import jwt

try:  # load .env if python-dotenv is available (it is, via fastapi[standard])
    from dotenv import load_dotenv
    # Load backend/.env regardless of the process CWD.
    _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_here, ".env"))
except Exception:
    pass


TAPIS_BASE_URL = os.getenv("TAPIS_BASE_URL", "https://icicleai.tapis.io").rstrip("/")
TAPIS_TENANT = os.getenv("TAPIS_TENANT", "icicleai")

# Confidential OAuth2 client credentials (registered with Tapis, with a
# callback_url of {APP_BASE_URL}/oauth2/callback).
TAPIS_CLIENT_ID = os.getenv("TAPIS_CLIENT_ID")
TAPIS_CLIENT_KEY = os.getenv("TAPIS_CLIENT_KEY")

# Direct-token mode: supply a Tapis access (and optional refresh) token via env to
# bypass the per-user OAuth flow entirely. When TAPIS_ACCESS_TOKEN is set, ALL
# Tapis calls (job submission + file browsing) use it regardless of which user
# owns the run — intended for dev / service-account use. If a refresh token AND a
# confidential client are also configured, it's auto-refreshed in-memory near
# expiry; otherwise it's used as-is until it lapses.
_env_token_state = {
    "access": os.getenv("TAPIS_ACCESS_TOKEN"),
    "refresh": os.getenv("TAPIS_REFRESH_TOKEN"),
}

# Refresh a token this many seconds before it actually expires, so a token that
# is about to lapse is renewed proactively rather than after a mid-request 401.
_EXPIRY_SKEW_SECONDS = 60


def oauth_client_configured() -> bool:
    """True when a confidential OAuth2 client is configured for real Tapis auth."""
    return bool(TAPIS_CLIENT_ID and TAPIS_CLIENT_KEY)


def env_token_mode() -> bool:
    """True when a Tapis token is supplied directly via env (TAPIS_ACCESS_TOKEN),
    bypassing per-user OAuth for all Tapis calls."""
    return bool(_env_token_state["access"])


def _jwt_seconds_left(token: str) -> float | None:
    """Seconds until a JWT's `exp`, or None if it can't be read."""
    try:
        exp = jwt.get_unverified_claims(token).get("exp")
        return None if exp is None else float(exp) - datetime.now(timezone.utc).timestamp()
    except Exception:
        return None


def get_env_token() -> str | None:
    """Return the env-provided Tapis access token, refreshing it in-memory when it
    nears expiry (requires TAPIS_REFRESH_TOKEN + a confidential client)."""
    access = _env_token_state["access"]
    if not access:
        return None
    left = _jwt_seconds_left(access)
    if left is not None and left < _EXPIRY_SKEW_SECONDS and _env_token_state["refresh"] and oauth_client_configured():
        tokens = _post_token_grant({
            "grant_type": "refresh_token",
            "refresh_token": _env_token_state["refresh"],
        })
        if tokens:
            _env_token_state["access"] = tokens["access_token"]
            if tokens.get("refresh_token"):
                _env_token_state["refresh"] = tokens["refresh_token"]
            return tokens["access_token"]
    return access


def _token_endpoint() -> str:
    return f"{TAPIS_BASE_URL}/v3/oauth2/tokens"


def _expiry_from_token_block(block: dict) -> datetime | None:
    """Derive a timezone-aware expiry from a Tapis access_token block.

    Tapis returns both `expires_in` (seconds) and `expires_at` (ISO string). We
    prefer `expires_in` (immune to client/server clock skew), falling back to
    parsing `expires_at`.
    """
    expires_in = block.get("expires_in")
    if isinstance(expires_in, (int, float)):
        return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    expires_at = block.get("expires_at")
    if expires_at:
        try:
            # Tapis uses e.g. "2024-01-01T00:00:00.000000+00:00" or trailing "Z".
            return datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _post_token_grant(payload: dict) -> dict | None:
    """POST a grant to the Tapis token endpoint with HTTP Basic client auth.

    Returns a normalized dict {access_token, refresh_token, expires_at} on
    success, or None on failure. Never logs secret material.
    """
    if not oauth_client_configured():
        print("[tapis_auth] No OAuth client configured (TAPIS_CLIENT_ID/KEY unset)")
        return None
    try:
        resp = httpx.post(
            _token_endpoint(),
            json=payload,
            auth=(TAPIS_CLIENT_ID, TAPIS_CLIENT_KEY),
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        access_block = result.get("access_token", {}) or {}
        refresh_block = result.get("refresh_token", {}) or {}
        access_token = access_block.get("access_token")
        if not access_token:
            print("[tapis_auth] Token grant returned no access_token")
            return None
        return {
            "access_token": access_token,
            "refresh_token": refresh_block.get("refresh_token"),
            "expires_at": _expiry_from_token_block(access_block),
        }
    except Exception as e:
        print(f"[tapis_auth] Token grant '{payload.get('grant_type')}' failed: {type(e).__name__}")
        return None


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict | None:
    """Exchange an OAuth2 authorization code for tokens (authorization_code grant).

    Returns {access_token, refresh_token, expires_at} or None on failure.
    """
    return _post_token_grant({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    })


def refresh_user_token(user, db) -> str | None:
    """Mint a fresh access token for `user` from their stored refresh token and
    persist the new access/refresh/expiry. Returns the new access token, or None
    if no refresh token is stored or the refresh grant fails (user must re-login).
    """
    if not user or not user.tapis_refresh_token:
        return None
    tokens = _post_token_grant({
        "grant_type": "refresh_token",
        "refresh_token": user.tapis_refresh_token,
    })
    if not tokens:
        return None
    _store_tokens(user, tokens)
    db.commit()
    return tokens["access_token"]


def _store_tokens(user, tokens: dict) -> None:
    """Copy a normalized token dict onto an AppUser (does not commit). A refresh
    grant may omit a new refresh_token; keep the existing one in that case."""
    user.tapis_access_token = tokens["access_token"]
    if tokens.get("refresh_token"):
        user.tapis_refresh_token = tokens["refresh_token"]
    user.tapis_token_expires_at = tokens.get("expires_at")


def store_tokens(user, tokens: dict, db) -> None:
    """Public helper for the login handler: persist exchanged tokens on a user."""
    _store_tokens(user, tokens)
    db.commit()


def _token_is_fresh(user) -> bool:
    """True if the user's stored access token exists and isn't within the skew
    window of expiring. A token with no known expiry is treated as stale so it
    gets validated/refreshed rather than trusted blindly."""
    if not user.tapis_access_token or not user.tapis_token_expires_at:
        return False
    expires_at = user.tapis_token_expires_at
    if expires_at.tzinfo is None:  # normalize naive DB values to UTC
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc) + timedelta(seconds=_EXPIRY_SKEW_SECONDS)


def get_token_for_user(user, db) -> str | None:
    """Return a usable access token for `user`, refreshing if it's expired/expiring.
    Returns None if the user has no tokens and can't refresh (must re-login)."""
    # Direct-token (env) mode overrides per-user resolution entirely.
    if env_token_mode():
        return get_env_token()
    if user is None:
        return None
    if _token_is_fresh(user):
        return user.tapis_access_token
    return refresh_user_token(user, db)


def get_token_for_run(run_id: int) -> str | None:
    """Resolve the access token of the run's owner. Called by the DBOS engine,
    which has no request/session context — it opens its own short DB session,
    resolves and (if needed) refreshes the token, and returns it. Resolving fresh
    at each call lets long durable workflows survive access-token expiry.
    """
    # Direct-token (env) mode: use the env token for any run, no DB lookup needed.
    if env_token_mode():
        return get_env_token()

    # Imported lazily to avoid importing the app DB layer at module import time.
    from db import SessionLocal
    from models import PipelineRun, AppUser

    db = SessionLocal()
    try:
        run = db.query(PipelineRun).filter(PipelineRun.run_id == run_id).first()
        if not run or not run.user_id:
            return None
        user = db.query(AppUser).filter(AppUser.user_id == run.user_id).first()
        return get_token_for_user(user, db)
    finally:
        db.close()


def username_from_jwt(token: str) -> str | None:
    """Extract the Tapis username from a JWT without verifying its signature
    (the token was just issued to us by Tapis over TLS). Falls back to the
    userinfo endpoint if the claim can't be read."""
    try:
        claims = jwt.get_unverified_claims(token)
        username = claims.get("tapis/username") or claims.get("sub")
        if username:
            return username
    except Exception:
        pass
    return validate_token(token).get("username")


def validate_token(token: str) -> dict:
    """Check a token against the Tapis userinfo endpoint. Returns a small dict
    {valid: bool, username: str|None, detail: str} — never includes the token."""
    url = f"{TAPIS_BASE_URL}/v3/oauth2/userinfo"
    try:
        resp = httpx.get(url, headers={"X-Tapis-Token": token}, timeout=30)
        if resp.status_code == 200:
            result = resp.json().get("result", {})
            return {
                "valid": True,
                "username": result.get("username"),
                "detail": "token accepted by Tapis userinfo",
            }
        return {"valid": False, "username": None, "detail": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"valid": False, "username": None, "detail": f"{type(e).__name__}"}


def describe_credentials() -> dict:
    """Report how Tapis auth is configured. Safe to log — no secret material."""
    use_mock = os.getenv("TAPIS_USE_MOCK", "false").lower() == "true"
    if use_mock:
        return {"mode": "mock", "reason": "TAPIS_USE_MOCK=true"}
    if env_token_mode():
        return {
            "mode": "env-token",
            "reason": "TAPIS_ACCESS_TOKEN set — using direct token for all Tapis calls",
            "auto_refresh": bool(_env_token_state["refresh"] and oauth_client_configured()),
            "base_url": TAPIS_BASE_URL,
            "tenant": TAPIS_TENANT,
        }
    if not oauth_client_configured():
        return {"mode": "mock", "reason": "no OAuth client configured (set TAPIS_CLIENT_ID/KEY)"}
    return {
        "mode": "oauth",
        "client_id": TAPIS_CLIENT_ID,
        "base_url": TAPIS_BASE_URL,
        "tenant": TAPIS_TENANT,
    }
