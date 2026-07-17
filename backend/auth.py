"""Tapis OAuth2 authorization-code login flow.

Replaces the previous hardcoded-JWT mock. The browser hits /login, we redirect to
Tapis' authorize endpoint, Tapis redirects back to /oauth2/callback with a code,
we exchange it (confidential client, see engine.tapis_auth) for an access +
refresh token, upsert the AppUser, persist the tokens, and set a signed session
cookie (SessionMiddleware, configured in main.py). The engine later resolves each
run owner's token from the DB.

Local dev without a registered Tapis client: set TAPIS_USE_MOCK=true and /login
short-circuits to a mock user session so the app stays runnable.
"""
import os
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from db import get_db
from models import AppUser, Team
from engine import tapis_auth

router = APIRouter()

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


@router.get("/login")
def login(request: Request):
    """Start the Tapis OAuth2 authorization-code flow.

    In mock mode, skip Tapis entirely: sign in a local mock user and bounce back
    to the frontend so the app is usable without a registered client.
    """
    if _use_mock():
        db = next(get_db())
        try:
            user = _upsert_user(db, "mock_user", "mock@example.com")
            request.session["username"] = user.username
        finally:
            db.close()
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


@router.get("/logout")
def logout(request: Request):
    """Clear the session and return to the frontend."""
    request.session.clear()
    return RedirectResponse(url=FRONTEND_URL)


@router.get("/me")
def me(request: Request):
    """Report the currently signed-in user (used by the frontend to show login
    state). 401 when there is no active session."""
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": username}
