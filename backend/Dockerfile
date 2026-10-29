# Harvest Workflow Orchestrator — backend (FastAPI + DBOS)
#
# Build from the repo root, passing the runner source as a named context:
#   docker build --build-context runner=runner -t harvest-backend backend/
# (Without --build-context the runner comes from the published runner image,
# which can lag runner/wf_runner; see the `runner` stage below.)
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

# --- runner: the wf_runner package bundled into deployment zips --------------
# Laid out like the repo's runner/ directory (wf_runner/ at its root), so
# `--build-context runner=runner` replaces this whole stage with the live source.
# Without it, fall back to the published runner image -- possibly older than
# runner/wf_runner, but never missing. That image is amd64-only; fine on any
# build host, as only its pure-Python files are copied out.
ARG RUNNER_IMAGE=ghcr.io/icicle-ai/workflow-runner:v0.1.3
FROM --platform=linux/amd64 ${RUNNER_IMAGE} AS runner-image
FROM scratch AS runner
COPY --from=runner-image /app/wf_runner /wf_runner

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

# The runner source, which engine/deployment.py copies into every deployment zip
# so `run.sh` can `python3 -m wf_runner` on the user's node. This image is built
# from backend/ alone, so ../runner isn't in the build context; it comes from the
# `runner` stage at the top. Without this the zip ships no wf_runner/ and run.sh
# dies with "No module named wf_runner".
COPY --from=runner --chown=app:app /wf_runner /app/runner/wf_runner
ENV WF_RUNNER_SOURCE_DIR=/app/runner

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
