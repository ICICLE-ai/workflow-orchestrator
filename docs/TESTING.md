# Testing

This document describes the testing and verification model for this repository.

It is written to be adopted **unchanged** across our repositories. The model
itself — the four levels, the merge and deployment criteria, and the user test
documentation rules — is intended to read identically everywhere. Three sections
are repo-specific and are rewritten per repository: *Level 0* (the local
commands), *Level 1* (the workflow inventory), and *Level 3* (the deployment
commands). Every command and workflow claim below was checked against the files
in this repository; where a claim depends on a file, that file is named.

## Current position on unit tests

**This repository has no unit tests, and that is a deliberate decision, not an
oversight.** Writing a unit suite for the existing surface area — the DBOS
execution engine, the step registry, the Tapis integration, and the canvas UI —
is a large up-front cost that has not been funded yet. Unit tests will be added
later, and when they are, running them will become a blocking CI gate.

What is deferred is **automated** testing, not verification. Until a unit suite
exists, the evidence that a change works is manual verification, carried out by
the contributor and recorded in a user test document under
[`user-tests/`](user-tests/). That record is **not optional** — it is the only
artifact this project has that says a change was exercised at all. A pull
request that changes behaviour without one is incomplete in the same way a pull
request that broke the build is incomplete.

Until then:

- **Keep logic in small pure functions.** Push decisions — validation, mapping,
  path and payload construction, DAG ordering — into functions that take values
  and return values, separate from the FastAPI route, the DBOS step, or the
  React component that calls them. This is the single thing that makes the later
  test suite cheap instead of expensive.
- **Do not introduce a test framework in an unrelated pull request.** Adding
  pytest or Vitest changes the dependency set, the CI configuration, and the
  contributor setup path. It is its own change, with its own review.
- **Volunteered tests are welcome** — see the contribution pathway in
  [CONTRIBUTING.md](../CONTRIBUTING.md). Ask a maintainer where they should go
  before writing them, so the first tests land in the layout the eventual suite
  will use rather than somewhere that has to be moved.
- **Record verification gaps in the pull request.** If you could not exercise
  part of a change — no Tapis allocation, no GPU system, no way to reproduce a
  path — say so in the pull request and in the *Not tested* section of the user
  test document. An honest gap is reviewable; a silent one is not.

### Intended future tooling

Nothing in this table is installed, configured, or running today. It records the
intended direction so that contributions do not each pick a different tool.

| Area | Intended tooling | Scope | Status |
| --- | --- | --- | --- |
| Backend (`backend/`) | pytest | Pure helpers first (port resolution, `step.json` parsing, payload rendering), then route-level tests through FastAPI's `TestClient` against a disposable database. | Not yet in effect — no test framework is installed. |
| Backend job scripts (`jobs/`) | pytest | The pure helpers inside each job's Python entrypoint. These files are not even compiled by CI today. | Not yet in effect. |
| Frontend (`frontend/`) | Vitest + React Testing Library | Hooks and non-canvas components; canvas behaviour is likely to stay manual for longer. | Not yet in effect. |
| Frontend end-to-end | Playwright | One happy-path run of build → configure → execute against a mock Tapis backend. | Not yet in effect. |
| Frontend static typing | `npm run typecheck` (already present) | Already runs in CI, but with `continue-on-error: true` because of pre-existing errors. Becomes a blocking gate when the backlog reaches zero. | Present, not blocking. |
| Linting / formatting | Not chosen | Neither language has a linter or formatter configured in this repository. | Not yet in effect. |

## Levels of verification

| Level | What it is | Who runs it | When | Blocks merge |
| --- | --- | --- | --- | --- |
| **0** | Local pre-submit checks — the same install, build, compile and import commands CI runs, on your own machine. | The contributor. | Before opening a pull request, and again after each substantive push. | Not directly. CI runs the same commands at Level 1, so skipping Level 0 only delays the failure. |
| **1** | Automated CI gates — the GitHub Actions workflows in [`.github/workflows/`](../.github/workflows/). | GitHub Actions. | Every pull request, every push to `main`, plus a weekly secret rescan. | Yes, except the frontend typecheck step, which is explicitly non-blocking. |
| **2** | Manual functional verification — exercising the change by hand and recording it as a user test document in [`user-tests/`](user-tests/). | The contributor, checked by the reviewer. | Whenever the change can affect observable behaviour (see the matrix below). | Yes, when it applies. |
| **3** | Deployment verification — building and running the container images and the database the same way a deployment does. | The contributor for deployment-surface changes; the release manager for a release. | Changes to Dockerfiles, compose, dependencies, or environment contract; and every release. | Yes for those changes. Required before a release regardless. |

## Which levels apply to my change?

Level 1 runs on everything — it is not opt-in. The other columns say what you
are responsible for.

| Change type | Level 0 | Level 1 | Level 2 | Level 3 |
| --- | --- | --- | --- | --- |
| Documentation only | Not required — nothing is built. | Automatic. | Not required. | No. |
| Refactor, no intended behaviour change | Required. | Automatic. | Required, but short: exercise the refactored path and one adjacent path to show nothing moved. | No. |
| Behaviour change (API, engine, step, UI) | Required. | Automatic. | Required, full document — happy path, edge case, failure case, regression check. | Only if it also changes the deployment surface. |
| Dependency change (`package.json`, `package-lock.json`, `requirements.txt`, `pyproject.toml`, `uv.lock`) | Required, from a clean install. | Automatic. | Required: exercise the feature that uses the dependency. | Required — images install from the lockfiles, not from your machine. |
| Build, container, or deployment change (Dockerfiles, `docker-compose.yml`, workflows, environment variables) | Required. | Automatic. | Required if a user-visible path changes. | Required. |
| Release | Required. | Automatic. | The release aggregates the user test documents of the changes it contains. | Required, and recorded. |

## Level 0 — local pre-submit checks

*Repo-specific.* These are the commands CI actually runs, in the order it runs
them, taken from [`.github/workflows/build.yml`](../.github/workflows/build.yml).
The frontend and backend are independent CI jobs, so the order between the two
blocks does not matter; the order **within** each block does.

### Frontend

```bash
cd frontend

# `ci`, not `install`: it installs exactly what package-lock.json pins and fails
# if the lockfile and package.json disagree — which is the check CI relies on to
# catch a dependency change that was never locked. Requires a GitHub Packages
# token in ~/.npmrc (see the README's Frontend Setup): the five @icicle-ai/*
# packages 401 without one, even though they are public.
npm ci

npm run build

# Non-blocking in CI, and it will not be clean here either: there are 37
# pre-existing errors on main, 35 of them in app/routes/WorkflowCanvas.tsx.
# Run it to confirm your change did not add to that count.
#
# The script is `react-router typegen && tsc`, and the order is load-bearing:
# tsconfig.json includes .react-router/types/**/*, which typegen writes. Bare
# `tsc` on a fresh clone fails on missing route types.
npm run typecheck
```

### Backend

```bash
cd backend

# MUST be first. main.py initializes DBOS at import time (main.py:176), which
# connects to Postgres and runs its own migrations — so nothing below can even
# import the app without a reachable database. This publishes postgres:14 on
# host port 5433, matching the defaults in db.py.
docker compose up -d

pip install -r requirements.txt

# Catches syntax errors in files the import below never reaches: individual
# steps' handlers, the seed and clear scripts, seldom-imported modules.
python -m compileall -q . -x '\.venv|__pycache__'

# The real check: import the app exactly as `python main.py` would. Same
# environment CI uses. TAPIS_USE_MOCK keeps the import free of any outbound call
# to the Tapis tenant.
DB_HOST=localhost DB_PORT=5433 DB_NAME=harvest DB_USER=wo DB_PASSWORD=password \
TAPIS_USE_MOCK=true SESSION_SECRET=local-not-a-real-secret \
python -c "import main; print('OK: main.py imported; app =', type(main.app).__name__)"
```

The environment variables above are the same values as `db.py`'s defaults, so
once the compose database is up, `python main.py` also works with no environment
set — but set them explicitly when reproducing a CI failure, so you are running
what CI ran.

### Toolchain versions and where they are pinned

| Toolchain | Version | Pinned in |
| --- | --- | --- |
| Node.js | 22 | [`build.yml`](../.github/workflows/build.yml) (`actions/setup-node`), [`frontend/Dockerfile`](../frontend/Dockerfile) (`node:22-alpine`). There is no `engines` field in `package.json`. |
| npm dependencies | Exact, from the lockfile | [`frontend/package-lock.json`](../frontend/package-lock.json), installed with `npm ci`. |
| Python | 3.14 | [`build.yml`](../.github/workflows/build.yml) (`actions/setup-python`), [`backend/pyproject.toml`](../backend/pyproject.toml) (`requires-python = ">=3.14"`), [`backend/Dockerfile`](../backend/Dockerfile) (`python:3.14-slim-bookworm`). |
| Python dependencies | Minimum-version ranges for the pip path; exact for images | [`backend/requirements.txt`](../backend/requirements.txt) (pip path, used by CI and the README), [`backend/uv.lock`](../backend/uv.lock) (image builds). |
| PostgreSQL | 14 | [`build.yml`](../.github/workflows/build.yml) (`services.postgres`), [`backend/docker-compose.yml`](../backend/docker-compose.yml). |
| uv | 0.9.9 | [`backend/Dockerfile`](../backend/Dockerfile). Image builds only — CI installs with pip. |
| gitleaks | 8.30.1, with a pinned SHA-256 of the release tarball | [`secret-scan.yml`](../.github/workflows/secret-scan.yml). |
| TruffleHog | Action pinned to commit `bcfcf73`, binary version 3.97.0 | [`secret-scan.yml`](../.github/workflows/secret-scan.yml). |

## Level 1 — automated CI gates

*Repo-specific.* Three workflows run today.

| Workflow | File | Triggers | What it proves |
| --- | --- | --- | --- |
| Build | [`.github/workflows/build.yml`](../.github/workflows/build.yml) | `pull_request` (any base branch); `push` to `main` | The frontend installs from the lockfile and builds. The backend installs from `requirements.txt`, every source file compiles, and `main.py` imports against a real Postgres. |
| Repository health | [`.github/workflows/repository-health.yml`](../.github/workflows/repository-health.yml) | `pull_request`; `push` to `main` | A fixed list of governance and documentation files exists. |
| Secret Scan | [`.github/workflows/secret-scan.yml`](../.github/workflows/secret-scan.yml) | `push` to `main`; `pull_request`; `schedule` (Mondays 06:00 UTC); `workflow_dispatch` | No secret matching the gitleaks ruleset or TruffleHog's detectors is present in the scanned commit range. |

None of the three run on tag pushes, and only Secret Scan can be started
manually. Build and Repository health have no `workflow_dispatch` trigger, so
the only way to re-run them is to re-run a previous run or push a new commit.

### Build

Permissions are `contents: read` and `packages: read` — the latter because the
frontend's `@icicle-ai/*` dependencies come from GitHub Packages. Concurrency is
grouped per workflow and ref, and cancels in progress **only** for pull requests;
`main` builds are never cancelled, because they are the record for that commit.

**Job: Frontend build** (`ubuntu-latest`, working directory `frontend`)

1. `actions/checkout@v4`.
2. `actions/setup-node@v4` — Node 22, npm cache keyed on
   `frontend/package-lock.json`, `registry-url: https://npm.pkg.github.com` and
   `scope: @icicle-ai`. That writes an `.npmrc` into the runner's *user* config,
   so the project's `frontend/.npmrc` (which carries `legacy-peer-deps=true`)
   still applies — project config outranks user config.
3. `npm ci` with `NODE_AUTH_TOKEN: ${{ secrets.GITHUB_TOKEN }}`. This succeeds
   only while the `@icicle-ai/*` packages grant this repository access.
   **Pull requests opened from forks do not receive org package access or
   repository secrets, so this job cannot pass for them.** Branches pushed to
   this repository are unaffected.
4. `npm run build` (`react-router build`).
5. `npm run typecheck`, with `continue-on-error: true`. **This step cannot fail
   the job.** It is non-blocking because the command reports 37 pre-existing
   errors on `main` — 35 in `app/routes/WorkflowCanvas.tsx`, one each in
   `app/routes/_index.tsx` and `app/routes/runs.tsx` — so gating on it would
   fail every pull request regardless of its contents. It runs for visibility.

**Job: Backend build** (`ubuntu-latest`, working directory `backend`)

Runs with a `postgres:14` service container (`POSTGRES_USER=wo`,
`POSTGRES_DB=harvest`, published on 5433, health-checked with `pg_isready`),
mirroring `backend/docker-compose.yml` — because importing the app requires a
reachable database.

1. `actions/checkout@v4`.
2. `actions/setup-python@v5` — Python 3.14, pip cache keyed on
   `backend/requirements.txt`.
3. `pip install --upgrade pip` then `pip install -r requirements.txt`. This is
   the path the README documents for contributors, so CI exercises the same
   install route a new contributor takes — not the `uv.lock` route the images
   use.
4. `python -m compileall -q . -x '\.venv|__pycache__'`.
5. `python -c "import main"` with the database variables, `TAPIS_USE_MOCK=true`
   and a throwaway `SESSION_SECRET`.

**This job is a smoke gate, not a test suite.** It proves the application can be
installed and imported — DBOS initialization, the SQLAlchemy datasource, the
step registry module imports, and every `@DBOS.workflow` / `@DBOS.step`
registration in `engine/`. It never serves a request and asserts nothing about
behaviour. Note also that the FastAPI startup handler (`main.py:408`), which
creates and patches the schema and syncs the step registry from
`steps/*/step.json`, does **not** run under a bare import — so a malformed
`step.json` passes this job.

### Repository health

A single job, `required-project-files`: checkout, then a shell loop over a
hard-coded list of paths, printing each missing one and exiting non-zero if any
are absent. It proves those files **exist**; it reads nothing inside them. The
list currently covers `LICENSE`, `README.md`, `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, `SECURITY.md`, `CITATION.cff`,
`docs/RELEASE_CHECKLIST.md`, `docs/MAINTAINER_ROLES.md`, this document, and
`docs/user-tests/TEMPLATE.md`.

Unlike the other two workflows it declares no `concurrency` group, so superseded
runs on the same pull request are not cancelled.

### Secret Scan

Two jobs, both checking out with `fetch-depth: 0` so history is available.

**Job: `gitleaks`.** Installs gitleaks 8.30.1 from the GitHub release, verifying
the tarball against a pinned SHA-256 — the version is pinned rather than
resolved from the releases API because that call is unauthenticated and
rate-limited per runner IP. On a pull request it scans **only the commits the
pull request adds** (`gitleaks git . --log-opts "${BASE_SHA}..${HEAD_SHA}"`); on
any other event it scans the **full history**. `--redact` keeps findings out of
the log. Rules and allowlists come from [`.gitleaks.toml`](../.gitleaks.toml) at
the repository root.

**Job: `trufflehog`.** Guarded by `if: github.event_name == 'push' ||
github.event_name == 'pull_request'`, so it is **skipped on the weekly schedule
and on manual dispatch** — the periodic full-history rescan is the gitleaks job
alone. The action is pinned to the commit tagged v3.97.0 rather than `@main`.
`base` and `head` are deliberately unset so the action derives the range itself;
`extra_args: --results=verified,unknown` keeps findings whose verification was
inconclusive.

A green Secret Scan means no *detected* secret in the scanned range. It is not a
proof of absence: detectors have gaps, `.gitleaks.toml` contains explicit
allowlists, and on a pull request gitleaks looks only at that branch's commits.

### What CI does not check today

Read this list as the definition of what Level 2 and Level 3 exist to cover.

1. **Nothing asserts behaviour.** There are no unit, integration, or end-to-end
   tests anywhere in the repository.
2. **The backend is imported, never exercised.** No endpoint is called, no
   authentication path is followed, no DBOS workflow is executed, no step
   handler runs.
3. **The FastAPI startup handler never fires** in CI, so schema creation and
   patching and the `step.json` registry sync are unverified. A step definition
   that would be skipped with a `SKIPPING step` warning at startup still passes.
4. **The frontend bundle is built, never loaded.** No route is rendered, no
   component is mounted, no browser is involved.
5. **Type errors do not block.** `npm run typecheck` is `continue-on-error`.
6. **There is no linter or formatter** configured for either language.
7. **Neither Dockerfile is built in CI.** A change that breaks an image build is
   caught only by Level 3, locally.
8. **`jobs/` is never compiled.** `compileall` runs with `backend` as its
   working directory, so the job scripts under `jobs/` are not syntax-checked,
   built, or run by anything in CI.
9. **No dependency vulnerability scanning.** There is no Dependabot
   configuration, no `npm audit` step, and no `pip-audit` step.
10. **Nothing verifies that `requirements.txt`, `pyproject.toml` and `uv.lock`
    agree.** CI installs from `requirements.txt`; the backend image installs
    from `uv.lock`. The two can drift, and have.
11. **Nothing runs on tags or releases.** A tagged release gets no CI run of its
    own.
12. **Fork pull requests cannot pass the frontend job**, for the packages-access
    reason above.
13. **Documentation links are not checked.**

## Level 2 — manual functional verification

Because Level 1 stops at "it builds and imports", manual verification is where
this project establishes that a change works. The minimum bar for any change
that can affect observable behaviour:

- **Happy path.** The intended use of the change, end to end, through the real
  interface a user or caller would use — the Studio UI, or the HTTP API — not
  through a Python REPL.
- **At least one edge case.** Empty input, a missing optional field, the
  boundary value, the largest realistic input, a node with no upstream
  connection — whatever "unusual but legal" means for this change.
- **At least one failure case.** What happens when it goes wrong: bad
  credentials, an unreachable Tapis system, a malformed payload, a cancelled
  run. Confirm the failure is reported, not swallowed, and that the message
  identifies the problem.
- **A regression check on adjacent behaviour.** One thing you did *not* change
  that shares code with what you did — the neighbouring step type, the other
  route on the same page, saving as well as loading. This is what catches the
  "fixed one thing, broke another" class of change.
- **Screenshots for anything visually changed.** Before and after, where a
  before exists.
- **Written up and committed in the same pull request**, as
  `docs/user-tests/YYYY-MM-DD-<slug>.md`, using
  [`user-tests/TEMPLATE.md`](user-tests/TEMPLATE.md).

Verification you did not record did not happen, as far as review and release are
concerned.

## Level 3 — deployment verification

*Repo-specific.* The deployment surface is two container images and a Postgres
database. There are no Kubernetes or Helm manifests in this repository.

### Database

```bash
cd backend
docker compose up -d
docker compose ps
```

### Backend image

```bash
docker build -t harvest-backend backend/

# DB_HOST must be set: the default in db.py is localhost, which inside a
# container is the container itself. host.docker.internal reaches the compose
# database published on 5433.
docker run --rm -p 8002:8002 --env-file backend/.env \
  -e DB_HOST=host.docker.internal -e DB_PORT=5433 harvest-backend
```

The image installs from `pyproject.toml` and `uv.lock` with `uv sync --frozen`,
**not** from `requirements.txt`, and runs `uvicorn main:app --host 0.0.0.0` as a
non-root user — not `python main.py`, which binds `127.0.0.1` and would be
unreachable from the host.

### Frontend image

```bash
DOCKER_BUILDKIT=1 docker build -t harvest-frontend \
  --secret id=npmrc,src=$HOME/.npmrc \
  --build-arg VITE_BACKEND_URL=https://api.example.org frontend/

docker run --rm -p 3000:3000 harvest-frontend
```

The `--secret` mount is required — the build fails by name without it — because
GitHub Packages rejects anonymous npm reads with 401 even for public packages.

### Verification checklist

- [ ] **Both images build clean** from a state matching the pull request, with
      no cached layer hiding a dependency change. Use `--no-cache` when the
      change touches dependencies or a Dockerfile.
- [ ] **The stack answers.** The database accepts connections
      (`docker exec harvest_db pg_isready -U wo -d harvest`); the backend
      returns 200 on `GET http://localhost:8002/`; the frontend returns 200 on
      `GET http://localhost:3000/`. Note that this repository has **no dedicated
      health endpoint and no `HEALTHCHECK` instruction in either Dockerfile** —
      these are the checks that exist. For authentication problems specifically,
      `GET /auth-debug` reports what credentials actually arrived.
- [ ] **A workflow runs end to end** against the containers, not just against a
      development server.
- [ ] **New environment variables are documented and present everywhere.** A new
      backend variable belongs in [`backend/.env.example`](../backend/.env.example)
      *and* in the environment contract at the bottom of
      [`backend/Dockerfile`](../backend/Dockerfile). A new `VITE_*` variable must
      be added as an `ARG`/`ENV` pair in
      [`frontend/Dockerfile`](../frontend/Dockerfile) — Vite inlines these at
      **build** time, so passing one with `docker run -e` has no effect at all,
      and a variable that exists only in `frontend/.env` silently falls back to
      its hardcoded default inside the image.
- [ ] **Unset behaviour is sane.** Start the containers without the new variable
      and confirm the application either uses a documented, safe default or
      fails immediately with a message naming the variable. It must not start
      and then misbehave later.
- [ ] **No secrets in the images.** No token, key, or `.env` file baked into a
      layer; confirm with `docker history` and by checking that credentials
      arrive through `--env-file`, a secret mount, or the runtime environment.

## Merge criteria

A reviewer should be able to tick every applicable line before approving.

- [ ] All blocking CI checks are green: **Build** (frontend and backend jobs),
      **Repository health**, **Secret Scan**.
- [ ] The frontend typecheck step did not gain new errors relative to `main`
      (it does not fail the build; check the log).
- [ ] The pull request is one coherent change and references its issue.
- [ ] A user test document is committed in this pull request, or the pull
      request states why none applies (documentation-only, or a change the
      matrix exempts).
- [ ] The user test document actually covers the change: happy path, an edge
      case, a failure case, and a regression check.
- [ ] Screenshots are present for user-visible changes.
- [ ] Documentation is updated where behaviour, interfaces, configuration,
      installation, or limitations changed.
- [ ] New or changed dependencies are identified, and the lockfile that matches
      them is committed.
- [ ] New environment variables are documented in every place listed under
      Level 3.
- [ ] No secrets, private data, or unlicensed material are included.
- [ ] Known verification gaps are stated in the pull request.

**A pull request is not blocked for lacking unit tests.** There is no unit suite
to add to, and asking a contributor to invent one is out of scope for review.

**A pull request is blocked for a missing user test document when one applies.**
That is the substitute this project has chosen, and it is the only evidence a
reviewer has.

## Deployment criteria

Everything under *Merge criteria*, plus:

- [ ] Level 3 deployment verification has been performed **and recorded** — a
      user test document, or the release record, saying which images were built,
      from which commit, and what was exercised against them.
- [ ] The release is traceable: a tag identifying the source commit, and images
      identifiable back to that tag.
- [ ] [`docs/RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md) is complete.
- [ ] Release notes state the known test gaps **explicitly, including the
      absence of unit tests** and the items under *What CI does not check
      today* that are relevant to this release. Someone deciding whether to
      deploy this should not have to discover that from the repository.
- [ ] A rollback path is known and written down: the previous tag, the previous
      images, and whether the release includes a database change that a rollback
      would have to undo. `CITATION.cff` and `component.yaml` carry version
      metadata that a rollback must also account for.

## User test documentation

**Where they live:** [`docs/user-tests/`](user-tests/), one file per verified
change, committed in the pull request that makes the change.

**Naming:** `docs/user-tests/YYYY-MM-DD-<slug>.md` — the date the verification
was performed, and a short hyphenated slug describing the change, for example
`2026-08-19-step-registry-port-sync.md`. Start from
[`TEMPLATE.md`](user-tests/TEMPLATE.md).

**Required sections:**

| Section | Contents |
| --- | --- |
| Header | Date, tester, pull request or issue, commit SHA verified, overall result. |
| Environment | OS, browser, Node and Python versions, how the backend and frontend were run, database, Tapis mode (mock or real tenant). |
| Scope | One or two sentences: what this change does, and what you set out to verify. |
| Preconditions | The state the system had to be in first — services running, seeded data, credentials, an existing template or run. |
| Test cases | The happy path, as numbered rows: steps, expected, actual, pass/fail. |
| Edge cases | Unusual but legal inputs and states, same columns. |
| Failure cases | Deliberately induced failures, same columns. |
| Regression check | Adjacent behaviour you did not change, exercised to show it still works. |
| Evidence | Screenshots, log excerpts, run IDs, response bodies. Redacted. |
| Issues found | Anything that did not work, with whether it is fixed in this pull request or filed separately. |
| Not tested | What you deliberately or unavoidably did not cover, and why. |

**Rules:**

1. **Write steps a stranger can follow.** Name the URL, the button, the endpoint,
   the exact input. "Created a workflow" is not a step; "opened
   `http://localhost:5173`, dragged *Extract Frames* onto the canvas, set
   `input_dir` to `/home/mock/data`" is.
2. **Record the expected result before you run the step, not after.** Deciding
   what should happen only once you have seen what did happen is not a test.
3. **Record failures.** A document with only passes is either a trivial change
   or an incomplete record. If something failed and you fixed it, say so and
   record the re-run.
4. **Use public, synthetic, or de-identified data only.** No credentials, no
   tokens, no real user identifiers, no restricted datasets, no sensitive
   locations — including inside screenshots and log excerpts. Redact before
   committing, not after.
5. **Keep it to about a page.** This is a record of what was exercised, not a
   specification. If it is running long, the pull request is probably doing more
   than one thing.
6. **Never edit a past document.** They are a historical record of what was
   verified, when, at which commit. If something later turns out to be wrong,
   write a new document; do not rewrite the old one.
7. **Say what you did not test.** The *Not tested* section is the most useful
   part of the document for the next person, and for whoever writes the release
   notes.

## Adopting this model in another repository

**Copy verbatim:** *Current position on unit tests* (excluding the future
tooling table), *Levels of verification*, *Which levels apply to my change?*,
*Level 2*, *Merge criteria*, *Deployment criteria*, *User test documentation*,
and this section. Copy `docs/user-tests/TEMPLATE.md` and
`docs/user-tests/README.md` as they are.

**Adapt per repository:** the *Intended future tooling* table (the languages
present); *Level 0* (that repository's real install, build, typecheck and lint
commands, in its CI's order, with its pinned versions); *Level 1* (its actual
workflow inventory, its trigger gaps, and its own *What CI does not check today*
list); *Level 3* (its deployment surface and health checks); the required-files
list in its repository-health workflow; and the related-documents list.

**Invariants, whatever the stack:**

- Level 0 is exactly what CI runs, in CI's order. If it drifts, contributors
  stop trusting it and stop running it.
- Every claim about CI is verified against the workflow file. A testing document
  that describes CI inaccurately is worse than none.
- A gate that cannot fail the build is described as non-blocking, and a job that
  only installs and imports is called a smoke gate, not a test suite.
- Deferring unit tests never defers verification. The user test document is the
  substitute, and it is mandatory whenever behaviour can change.
- What CI does not check is written down, in the open, and kept current.
- Test records are immutable, dated, and traceable to a commit.

## Related documents

| Document | Purpose |
| --- | --- |
| [../README.md](../README.md) | Project overview and local setup. |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | How to contribute; links back here for the testing flow. |
| [../HOW_TO_USE.md](../HOW_TO_USE.md) | End-user walkthrough — the source for realistic Level 2 scenarios. |
| [user-tests/README.md](user-tests/README.md) | Why the user test record exists and how to add one. |
| [user-tests/TEMPLATE.md](user-tests/TEMPLATE.md) | The template to copy for each user test document. |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | Completed before a public release; referenced by *Deployment criteria*. |
| [MAINTAINER_ROLES.md](MAINTAINER_ROLES.md) | Who approves pull requests and releases. |
| [../SECURITY.md](../SECURITY.md) | Reporting a vulnerability — never in a public issue or a test document. |
| [adding-a-step-form.md](adding-a-step-form.md) | Step authoring, ports, run configuration, and the runtime execution model. |
| [adding-a-step-custom-ui.md](adding-a-step-custom-ui.md) | Replacing a step's generated form with a custom React panel. |
