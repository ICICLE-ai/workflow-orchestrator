"""Team-scoped secrets (e.g. Weights & Biases / Hugging Face API tokens).

Secrets are entered once via the dashboard's settings dropdown (/api/secrets)
and referenced by KEY from a step's config_schema (a field with
"type": "secret"). Only the key ever lands in a node's config_values / a run's
frozen_config / run_step.config — see workflows.py's _resolve_secrets, which
substitutes the real value into the Tapis job context only, right before
submission, and never writes it back to anything persisted.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _derive_fernet_key(raw: str) -> bytes:
    # SECRETS_ENCRYPTION_KEY isn't guaranteed to already be a 32-byte urlsafe-
    # base64 Fernet key, so derive one deterministically via SHA-256 — same
    # ergonomics as SESSION_SECRET (any string works).
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


_fernet = Fernet(_derive_fernet_key(os.getenv("SECRETS_ENCRYPTION_KEY", "dev-insecure-secrets-key-change-me")))


def encrypt_value(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    return _fernet.decrypt(token.encode()).decode()


def resolve_secret(team_id: int | None, key: str | None) -> str | None:
    """Look up and decrypt a team's secret by key. Called by the DBOS engine
    (no request/session context), so it opens its own short DB session — same
    pattern as tapis_auth.get_token_for_run."""
    if not team_id or not key:
        return None
    from db import SessionLocal
    from models import Secret

    db = SessionLocal()
    try:
        row = db.query(Secret).filter(Secret.team_id == team_id, Secret.key == key).first()
        return decrypt_value(row.encrypted_value) if row else None
    finally:
        db.close()


def get_run_team_id(run_id: int) -> int | None:
    """Resolve the team_id of the run's owner, for scoping secret lookups."""
    from db import SessionLocal
    from models import PipelineRun, AppUser

    db = SessionLocal()
    try:
        run = db.query(PipelineRun).filter(PipelineRun.run_id == run_id).first()
        if not run or not run.user_id:
            return None
        user = db.query(AppUser).filter(AppUser.user_id == run.user_id).first()
        return user.team_id if user else None
    finally:
        db.close()
