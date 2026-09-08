# Verification record

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
- 380 local automated checks passed after the latest review corrections. Twelve opt-in provider cases passed the same day with live synthetic input; four completed after a temporary rate limit cleared. Current review corrections have local database tests, including repeated DST hours, retained correction timezones, Telegram ID resets, stale follow-ups, failed restore-marker cleanup, old UUID corrections, corrected physiology before notification delivery, undo of acknowledged facts, export preservation, MCP cancellation, provider startup failures, expired polling offsets, restore commit failures, exclusive recovery publication, and database query overrides. The live button-refinement scenario was also rechecked with a diary exceeding the recent-context limit; it updated the existing episode without duplication. The new multiple-candidate close scenario also passed with live Gemini.

SELinux preparation is documented with the Docker Mount API limitation. The Compose configuration
was checked locally; deployment on an enforcing SELinux host remains unverified.

## Still required for full acceptance

- Finish all PR review windows, resolve substantive findings and merge the verified stack into main.
- Observe seven days of unattended synchronization. Deployment began on 2026-09-08; this cannot
  be validated by a short smoke test. The machine must stay awake with Docker running.

Personal insights require sufficient observed days/events. Insufficient evidence must produce
an explicit limitation rather than low-confidence health claims.
