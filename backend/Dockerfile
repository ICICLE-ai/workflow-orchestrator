# Harvest Workflow Orchestrator — backend (FastAPI + DBOS)
#
# Build from the backend/ directory:
#   docker build -t harvest-backend backend/
#
# Run (see the env contract at the bottom of this file):
#   docker run --rm -p 8002:8002 --env-file backend/.env \
#     -e DB_HOST=host.docker.internal -e DB_PORT=5433 harvest-backend
#
# Dependencies are installed with uv from uv.lock, so an image built today and
# one built in six months contain the same versions. requirements.txt is NOT
# used here: it has drifted from pyproject.toml (it lists Pillow but not dbos or
# pyogrio, without which the app cannot even import), so pyproject + uv.lock are
# the single source of truth.

# --- builder: resolve and install dependencies into a self-contained venv ----
FROM python:3.14-slim-bookworm AS builder

# uv ships as a static binary; copying it from the official image avoids a pip
# bootstrap and pins the tool itself.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer, kept separate from the source so editing a .py file doesn't
# reinstall geopandas. --frozen fails rather than silently re-resolving if
# uv.lock is out of date with pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# --- runtime -----------------------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

# libpq/GDAL/GEOS are NOT installed: psycopg2-binary, pyogrio and shapely all
# ship manylinux wheels with those libraries bundled. That's also why this is a
# glibc (slim) base rather than alpine — musl has no such wheels, so alpine
# would compile GDAL from source and produce a far larger, slower build.
# Adding a dependency that needs a real system library is the signal to revisit
# this, and the build will tell you loudly if so.

# Run as a non-root user. The app writes nothing to the image at runtime — all
# artifacts go to Tapis — so nothing here needs to be writable.
RUN useradd --create-home --uid 10001 app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app . /app

USER app

EXPOSE 8002

# NOT `python main.py`. That entrypoint binds 127.0.0.1 (deliberately, for local
# debugging), which inside a container means the port is unreachable from the
# host no matter how it's published. uvicorn is invoked directly so the bind
# address is 0.0.0.0, and with no --reload: the reloader's child process would
# sit under PID 1 and swallow signals, so `docker stop` would hang until it
# timed out and SIGKILLed.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]

# --- environment --------------------------------------------------------------
# Required at RUN time (see .env.example for the full contract):
#   DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD
#       Postgres connection (stock Postgres — no PostGIS extension needed).
#       Defaults in db.py target localhost:5433,
#       which inside a container is the container itself — always set DB_HOST.
#       The compose DB publishes 5433, so from Docker Desktop that's
#       DB_HOST=host.docker.internal DB_PORT=5433; on the same compose network
#       it's the service name with the INTERNAL port, DB_HOST=db DB_PORT=5432.
#   SESSION_SECRET      Signs the session cookie.
#   APP_BASE_URL        Public URL of THIS backend; the Tapis OAuth callback is
#                       built from it and must match the registered client.
#   FRONTEND_URL        Post-login redirect target, also added to the CORS
#                       allow-list — a mismatch is a browser CORS failure on
#                       every API call, not a subtle one.
#   TAPIS_CLIENT_ID / TAPIS_CLIENT_KEY, or TAPIS_ACCESS_TOKEN for direct-token
#                       mode, or TAPIS_USE_MOCK=true to run without credentials.
