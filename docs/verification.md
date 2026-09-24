# Verification record

## Universal release gate

The complete `UT-01`–`UT-53` evidence map is stored in
[`tests/universal_acceptance_manifest.json`](../tests/universal_acceptance_manifest.json) and is
validated by `tests/test_release_gate.py`. CI records the exact Git SHA and database revision
`c8f51d3a7e20` in `test-results/release-metadata.json`, then preserves that file beside JUnit and
coverage results. The commands are:

```sh
uv sync --locked --extra full
uv run python scripts/release_metadata.py > test-results/release-metadata.json
uv run ruff check .
uv run ruff format --check .
uv run pytest -q -ra --junitxml=test-results/pytest.xml --cov=garmin_ai --cov-branch
```

A separate clean CI job runs `uv sync --locked` without provider/channel extras and verifies the
core import, architecture boundaries and release manifest. The automated environment is a
disposable synthetic PostgreSQL/TimescaleDB. No live Garmin, Telegram, model-provider or personal
data call is part of this gate. Platform- or live-only skips remain visible with their reasons in
the JUnit and `pytest -ra` output.

The verified recovery policy is backup/restore or forward recovery. Direct downgrade of an old
binary over new custom data is not supported. No production database is touched, and WhatsApp is
not implemented or claimed.

The GA-01–GA-32 criterion status and the separate seven-day, off-host, device and provider
acceptance protocol are recorded in [operational-acceptance.md](operational-acceptance.md).
That protocol is planned work; the synthetic release gate does not execute it.

## Verified on 2026-09-08 (Europe/Bratislava)

- Real Garmin authentication and private ingestion verification completed. Account-specific
  availability and coverage evidence remain in local private reports.
- PostgreSQL/TimescaleDB migrations and real database tests cover idempotent replay, corrections,
  stale request rejection, DST, midnight intervals, undo, leases, API access and analysis limits.
- The actual owner paired the Telegram bot and received its initial reply. Deterministic diary,
  clarification, correction, no-repeat and proactive recovery flows have synthetic tests.
- Twelve live synthetic Gemini scenarios passed using the available `gemini-3.5-flash` model:
  coffee, ambiguous time, relative-time migraine, unknown medication, undo, compound diary entry,
  close/correct an episode, Russian voice transcription, button refinement without a duplicate, an urgent report bypassing a stalled diary update, selection of one of several open migraines in a large diary, and emergency screening of an oversized voice transcript. The initial relative-time failure
  was corrected by supplying the current instant in the user's timezone and then rechecked.
  The initial `gemini-3.8-flash` model exhausted its free quota; the API key was unchanged.
- The MCP stdio protocol was exercised through a real client. Its read tools, invalid-argument
  errors, creation and partial correction behavior are tested against a disposable database.
- An encrypted backup of the real local database was decrypted and restored into a separate
  disposable database. Every exported record matched; the verification database was removed.
- The Compose image was rebuilt and restarted on 2026-09-08. Database, API
  and worker all report healthy. Readiness returns 200; authenticated tools work and unauthenticated
  requests return 401. Existing measurements and activities survived the schema upgrade.
- The worker created a new backup in the separate configured directory. Its encrypted backup was restored into a fresh disposable database; every exported
  record matched. The backup also contains the coverage manifest and Garmin login tokens.
- 442 local automated checks passed after the latest review corrections. Twelve opt-in provider cases passed the same day with live synthetic input; four completed after a temporary rate limit cleared. Current review corrections have local database tests, including repeated DST hours, retained correction timezones, Telegram ID resets, stale follow-ups, failed restore-marker cleanup, old UUID corrections, corrected physiology before notification delivery, undo of acknowledged facts, export preservation, MCP cancellation, provider startup failures, expired polling offsets, restore commit failures, exclusive recovery publication, database query overrides and option values, upstream notification backlogs, concurrent diary edits during question delivery, and forced process termination across storage activation and erasure. Checks also cover failed synchronization gating, backup ordering, future migraine endings, lock-directory permissions, and case-insensitive storage aliases, same-slot recovery after suppressed context generation, acknowledgements concurrent with episode edits, actual terminal-failure timestamps, bounded caffeine absence intervals, and export/backup publication when hard links are unavailable. A fresh encrypted backup was restored into an isolated database after these corrections, with every record matching. The live button-refinement scenario was also rechecked with a diary exceeding the recent-context limit; it updated the existing episode without duplication. The new multiple-candidate close scenario also passed with live Gemini.

SELinux preparation is documented with the Docker Mount API limitation. The Compose configuration
was checked locally; deployment on an enforcing SELinux host remains unverified.

## Still required for full acceptance

- Finish all PR review windows, resolve substantive findings and merge the verified stack into main.
- Observe seven days of unattended synchronization. Deployment began on 2026-09-08; this cannot
  be validated by a short smoke test. The machine must stay awake with Docker running.

Personal insights require sufficient observed days/events. Insufficient evidence must produce
an explicit limitation rather than low-confidence health claims.

Latest review regressions additionally cover webhook backlogs returning after startup,
fixed proactive recovery deadlines, Garmin writes waiting for context delivery,
reciprocal backup/synchronization exclusion, scheduled backup dates across midnight,
activation-cleanup compensation, stable lock paths, junction rejection, and invalid
preserved timezones. Windows no-follow handle behavior is tested with a mocked API;
execution on a native Windows host remains unverified. These corrections await
review and coordinated deployment to GCP.
