"""Tapis credential resolution.

Resolves a Tapis JWT to use as the X-Tapis-Token header, from environment /
.env, in priority order:
  1. TAPIS_TOKEN  -> used directly.
  2. TAPIS_USERNAME + TAPIS_PASSWORD -> exchanged for a JWT via the Tapis
     tokens API (password grant).
  3. Neither -> returns None, and callers fall back to the mock client.

Secrets are never printed. `describe_credentials()` reports only what KIND of
credential is configured and whether a token could be obtained/validated.
"""
import os

import httpx

try:  # load .env if python-dotenv is available (it is, via fastapi[standard])
    from dotenv import load_dotenv
    # Load backend/.env regardless of the process CWD.
    _here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_here, ".env"))
except Exception:
    pass


TAPIS_BASE_URL = os.getenv("TAPIS_BASE_URL", "https://icicleai.tapis.io").rstrip("/")
TAPIS_TENANT = os.getenv("TAPIS_TENANT", "icicleai")

_cached_token: str | None = None


def _mint_token_from_password(username: str, password: str) -> str | None:
    """Exchange username/password for a short-lived JWT via the Tapis tokens API."""
    url = f"{TAPIS_BASE_URL}/v3/oauth2/tokens"
    payload = {
        "username": username,
        "password": password,
        "grant_type": "password",
    }
    try:
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # Tapis returns result.access_token.access_token
        return (
            data.get("result", {})
            .get("access_token", {})
            .get("access_token")
        )
    except Exception as e:
        print(f"[tapis_auth] Could not mint token from password: {type(e).__name__}")
        return None


def get_token(force_refresh: bool = False) -> str | None:
    """Return a usable Tapis JWT, or None if no credentials are configured."""
    global _cached_token
    if _cached_token and not force_refresh:
        return _cached_token

    token = os.getenv("TAPIS_TOKEN")
    if token:
        _cached_token = token.strip()
        return _cached_token

    username = os.getenv("TAPIS_USERNAME")
    password = os.getenv("TAPIS_PASSWORD")
    if username and password:
        _cached_token = _mint_token_from_password(username, password)
        return _cached_token

    return None


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
    """Report what kind of credential is configured and whether it works.
    Safe to log — contains no secret material."""
    use_mock = os.getenv("TAPIS_USE_MOCK", "false").lower() == "true"
    if use_mock:
        return {"mode": "mock", "reason": "TAPIS_USE_MOCK=true"}

    if os.getenv("TAPIS_TOKEN"):
        kind = "token"
    elif os.getenv("TAPIS_USERNAME") and os.getenv("TAPIS_PASSWORD"):
        kind = "password"
    else:
        return {"mode": "mock", "reason": "no TAPIS_TOKEN or TAPIS_USERNAME/PASSWORD set"}

    token = get_token()
    if not token:
        return {"mode": "mock", "reason": f"{kind} present but no JWT could be obtained"}

    check = validate_token(token)
    return {
        "mode": "real" if check["valid"] else "mock",
        "credential_kind": kind,
        "base_url": TAPIS_BASE_URL,
        "tenant": TAPIS_TENANT,
        "token_valid": check["valid"],
        "username": check["username"],
        "detail": check["detail"],
    }
