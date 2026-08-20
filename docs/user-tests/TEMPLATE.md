<!--
  Copy this file to docs/user-tests/YYYY-MM-DD-<slug>.md — the date you performed
  the verification, and a short hyphenated slug for the change, e.g.
  2026-08-19-step-registry-port-sync.md

  Fill in every section. Delete the HTML comments as you go, but keep the
  headings even when a section is empty — write "None" rather than removing it,
  so a reader can tell the difference between "nothing to report" and "not
  considered".

  Rules are in docs/TESTING.md, section "User test documentation". The two that
  are most often got wrong: write the expected result BEFORE you run the step,
  and use public, synthetic, or de-identified data only — including inside
  screenshots and log excerpts.
-->

# User test — <short title of the change>

| Field | Value |
| --- | --- |
| Date | <!-- YYYY-MM-DD, the date the verification was performed --> |
| Tester | <!-- Name or GitHub handle --> |
| PR / issue | <!-- #123, or a link --> |
| Commit SHA | <!-- The exact commit you tested; `git rev-parse --short HEAD` --> |
| Result | <!-- Pass / Pass with issues / Fail --> |

## Environment

<!-- Enough for someone to reproduce this. Delete rows that do not apply. -->

| Field | Value |
| --- | --- |
| OS | <!-- e.g. macOS 15.5 --> |
| Browser | <!-- e.g. Chrome 141; N/A for backend-only changes --> |
| Node.js | <!-- `node --version`; CI uses 22 --> |
| Python | <!-- `python --version`; CI uses 3.14 --> |
| Backend run as | <!-- `python main.py`, or the harvest-backend container --> |
| Frontend run as | <!-- `npm run dev`, or the harvest-frontend container --> |
| Database | <!-- backend/docker-compose.yml postgres:14 on 5433, or other --> |
| Tapis mode | <!-- TAPIS_USE_MOCK=true, or the real tenant + system used --> |

## Scope

<!--
  One or two sentences. What does this change do, and what did you set out to
  verify? Not a restatement of the PR description — say what "working" means
  here.
-->

## Preconditions

<!--
  The state the system had to be in before the first test case: services
  running, database seeded or empty, credentials configured, an existing
  template/run/file that the steps below depend on.
-->

1. 
2. 

## Test cases — happy path

<!--
  The intended use of the change, through the real interface. Steps a stranger
  can follow: name the URL, the control, the endpoint, the exact input.
  Fill "Expected" before running the step.
-->

| # | Steps | Expected | Actual | Pass/Fail |
| --- | --- | --- | --- | --- |
| 1 |  |  |  |  |
| 2 |  |  |  |  |

## Edge cases

<!--
  Unusual but legal input or state: empty value, missing optional field, a
  boundary value, the largest realistic input, a node with nothing connected
  upstream. At least one.
-->

| # | Steps | Expected | Actual | Pass/Fail |
| --- | --- | --- | --- | --- |
| 1 |  |  |  |  |

## Failure cases

<!--
  Deliberately induced failures: bad credentials, unreachable system, malformed
  payload, cancelled run. Check the failure is reported rather than swallowed,
  and that the message identifies the problem. At least one.
-->

| # | Steps | Expected | Actual | Pass/Fail |
| --- | --- | --- | --- | --- |
| 1 |  |  |  |  |

## Regression check

<!--
  Something you did NOT change that shares code with what you did — the
  neighbouring step type, the other route on the same page, saving as well as
  loading. State what you exercised and that it still behaves as before.
-->

| # | Adjacent behaviour | Steps | Result |
| --- | --- | --- | --- |
| 1 |  |  |  |

## Evidence

<!--
  Screenshots (before and after for visual changes), log excerpts, run IDs,
  response bodies. Redact tokens, credentials, real usernames, and sensitive
  locations BEFORE committing. Put images in docs/images/ and link them.
-->

## Issues found

<!--
  Anything that did not work. For each: what happened, and whether it is fixed
  in this PR or filed as a separate issue (link it). Write "None" if there were
  none.
-->

## Not tested

<!--
  What you deliberately or unavoidably did not cover, and why — no allocation on
  the real tenant, no GPU system available, a path that needs production data.
  This section is the most useful part of the document for the next person and
  for the release notes. Do not leave it empty.
-->
