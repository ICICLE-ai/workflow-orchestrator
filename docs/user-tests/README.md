# User tests

This directory is this project's test record.

The repository has no unit tests — a deliberate, temporary position explained in
[../TESTING.md](../TESTING.md). CI proves that the frontend builds and that the
backend installs and imports; it asserts nothing about behaviour. Until an
automated suite exists, the evidence that a change works is manual verification,
performed by the contributor and written down here. Each file records what was
exercised, at which commit, by whom, and what was left untested.

## Adding one

1. Copy [TEMPLATE.md](TEMPLATE.md) to `YYYY-MM-DD-<slug>.md` — the date you
   performed the verification and a short hyphenated slug for the change, e.g.
   `2026-08-19-step-registry-port-sync.md`.
2. Exercise the change by hand: the happy path, at least one edge case, at least
   one failure case, and a regression check on adjacent behaviour. Screenshots
   for anything visually changed.
3. Fill in every section, including *Not tested*.
4. Commit it in the same pull request as the change, and link it from the pull
   request's *Validation performed* section.

## Rules

1. Write steps a stranger can follow — the URL, the control, the endpoint, the
   exact input.
2. Record the expected result before running the step, not after.
3. Record failures, not just passes. If you fixed something, record the re-run.
4. Public, synthetic, or de-identified data only. No credentials, tokens, real
   user identifiers, restricted datasets, or sensitive locations — including
   inside screenshots and log excerpts.
5. Keep it to about a page.
6. Never edit a past document. They are a record of what was verified at a
   commit; write a new one instead.
7. Say what you did not test.

Full detail, and the four levels of verification these documents sit in, are in
[../TESTING.md](../TESTING.md).
