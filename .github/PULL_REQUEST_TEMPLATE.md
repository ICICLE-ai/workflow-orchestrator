## Purpose

Describe the user or maintainer problem addressed by this pull request. Link the related issue.

## Change summary

- 

## Validation performed

See [docs/TESTING.md](../docs/TESTING.md). Tick what applies; strike out what does not and say why.

Local checks (the same commands CI runs):

- [ ] `frontend/`: `npm ci` and `npm run build` succeed.
- [ ] `frontend/`: `npm run typecheck` adds no new errors (it is non-blocking in CI; 37 pre-existing errors on `main`).
- [ ] `backend/`: `docker compose up -d`, then `pip install -r requirements.txt`.
- [ ] `backend/`: `python -m compileall -q . -x '\.venv|__pycache__'` is clean.
- [ ] `backend/`: the app imports — `DB_HOST=localhost DB_PORT=5433 DB_NAME=harvest DB_USER=wo DB_PASSWORD=password TAPIS_USE_MOCK=true SESSION_SECRET=local-not-a-real-secret python -c "import main"`.
- [ ] Not applicable — this pull request changes documentation only.

Manual verification:

- [ ] I exercised the changed example, workflow, configuration, or interface by hand: happy path, an edge case, a failure case, and a regression check on adjacent behaviour.
- [ ] **User test document:** `docs/user-tests/YYYY-MM-DD-<slug>.md` — link it here, or state why none is needed: 
- [ ] I updated user and developer documentation where needed.

Deployment (Dockerfile, compose, dependency, or environment-variable changes only):

- [ ] Both images build (`docker build -t harvest-backend backend/`; `DOCKER_BUILDKIT=1 docker build -t harvest-frontend --secret id=npmrc,src=$HOME/.npmrc frontend/`) and the containers answer on `:8002/` and `:3000/`.
- [ ] New environment variables are documented in `backend/.env.example` and the `backend/Dockerfile` env contract, or added as `ARG`/`ENV` in `frontend/Dockerfile` for `VITE_*`, with sane behaviour when unset.

## Screenshots

Add screenshots or screen recordings for any user-visible change. Delete this section if it does not apply.

## Contribution readiness

- [ ] I identified new or changed dependencies.
- [ ] I identified data, provenance, privacy, security, and credential implications.
- [ ] I identified a maintenance owner or explained why this is not yet known.
- [ ] I did not include secrets, private keys, proprietary data, restricted data, or unauthorized third-party material.
- [ ] I have the right to submit this contribution under the repository license.

## Reviewer notes

Describe any limitations, follow-up work, compatibility concerns, or release notes needed.

State any **known verification gaps** — what you could not exercise, and why (no allocation, no GPU system, a path that needs production data). This repository has no unit tests, so an unstated gap is invisible to review.
