# Verification record

## Verified on 2026-09-08 (Europe/Bratislava)

- Real Garmin login and private 14-date investigation completed. Only three dates contained
  usable daily summaries at the initial snapshot; two activities and their original FITs were
  retained. Nonempty scaffolding from other dates was not treated as observed health data.
- PostgreSQL/TimescaleDB migrations and real database tests cover idempotent replay, corrections,
  stale request rejection, DST, midnight intervals, undo, leases, API access and analysis limits.
- The actual owner paired the Telegram bot and received its initial reply. Deterministic diary,
  clarification, correction, no-repeat and proactive recovery flows have synthetic tests.
- Gemini successfully interpreted coffee, ambiguous coffee time and undo in live synthetic checks.
  Further cases and voice transcription encountered HTTP 429 quota errors. These checks are
  **incomplete**; mock tests are not a claim of successful live voice recognition.
- The MCP stdio protocol was exercised through a real client. Its read tools, invalid-argument
  errors, creation and partial correction behavior are tested against a disposable database.
- An encrypted backup of the real local database was decrypted and restored into a separate
  disposable database. Every exported record matched; the verification database was removed.
- Local Compose database, API and worker started; readiness and authenticated access were checked.
  Daily backup and Garmin jobs completed under the container runtime.

## Still required for full acceptance

- Complete blocked live Gemini text/voice checks after quota becomes available.
- Finish all PR review windows, resolve substantive findings and merge the verified stack into main.
- Verify the final container image after all review changes and a complete restart.
- Observe seven days of unattended synchronization. Deployment began on 2026-09-08; this cannot
  be validated by a short smoke test. The machine must stay awake with Docker running.

Long-term personal insights remain unavailable until enough observed days/events exist. That is
an evidence limitation, not a reason to produce low-confidence health claims.
