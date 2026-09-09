# Development workflow

The owner authorizes creating non-draft PRs, requesting review and merging verified work.
Define completion criteria, implement a coherent feature, run relevant tests and inspect the diff.
After **every push** to a PR, comment `@codex review`. Continue the next independent feature.

Wait at least 30 minutes after the most recent push/review request. Inspect issue comments,
reviews and inline findings for that revision. Address substantive findings and request another
review after a push; the waiting period restarts. Passing checks plus no unresolved findings
(or an explicit clean review) permits merging into `main`. Do not merge into an unfinished feature.

Dependent PRs may temporarily target the preceding feature for a readable diff. Retarget them
to `main` after the parent merges. Preserve ancestry when merging stacked changes. Re-check the
resulting diff and checks after retargeting. No routine approval question is needed.

Run `uv run ruff check .`, `uv run ruff format --check .`, `git diff --check`, and relevant pytest
cases. Database tests require a disposable PostgreSQL/TimescaleDB ending in `_test`; absence
means skipped tests, not a database validation pass. CI provides the service.

Only synthetic or explicitly redacted fixtures belong in Git. Credentials, raw Garmin payloads,
FIT exports, original voice recordings, local exports, backups and health rows stay ignored.
A private GitHub repository is not a credential store. Inspect staged content before pushing.

Live provider tests are opt-in and may be blocked by API quota. Record those limitations honestly.
Do not mark a feature complete on the strength of mocked tests alone when a real check is possible.
