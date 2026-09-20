# Universal core foundation (UNI-01)

This phase records the current behavior and the boundaries that later tracker and
channel work must preserve. It does not add custom tracker storage, migrate production
data, remove Telegram, or implement another messenger.

## Reproducible baseline

- Source commit: `2d2c20c6d775b7d98c928f8c6399525b25f32ec4` (`main`, matching the universal review baseline).
- Database: disposable local PostgreSQL/TimescaleDB database named `garmin_ai_uni01_test`.
- Command:

  ```sh
  GA_REQUIRE_TEST_DB=1 \
  GA_TEST_DATABASE_URL='postgresql+psycopg://.../garmin_ai_uni01_test' \
  uv run pytest -q -ra --junitxml=test-results/uni-01-baseline.xml
  ```

- Result on 2026-09-20: `2309 passed, 12 skipped` in 178.91 seconds.
- All skips were the explicitly opt-in live Gemini scenarios in `tests/test_gemini_live.py`.
  No live Garmin, Telegram, Gemini, production database, or personal health data was used.
- The generated JUnit path is ignored by Git. CI publishes its own JUnit artifact; the
  command above reproduces the local baseline without committing machine-specific output.

## Dependency boundaries

The migration is incremental; files do not need to move before responsibilities are
separated. The intended dependency direction is:

```text
domain (events, metrics, time, evidence)
  <- application (commands, dialogue, forms, analysis orchestration)
  <- adapters/composition (Telegram, API, MCP, Garmin, model providers, runtime)
  <- infrastructure (PostgreSQL, archive, jobs)
```

Domain and application code must not import the Telegram SDK. Telegram SDK imports are
currently limited to `telegram.py`, `telegram_format.py`, `pairing.py`, and the transitional
composition root `runtime.py`; UNI-11 removes the runtime exception when Telegram becomes a
complete adapter.

The legacy `TelegramUpdate` storage row is still read by `jobs.py`, `personal_goals.py`,
`proactive.py`, and `retention.py`, plus the current runtime/Telegram transport path. These
are exact UNI-10 exceptions, not a package-wide allowlist. The architecture tests fail if a
new file imports that transport DTO or a non-adapter imports the Telegram SDK.

## Current authoritative paths

An inbound Telegram update follows one write path:

```text
save_update (authenticate + deduplicate update)
  -> durable telegram_update/control job
  -> process_message / deterministic command or validated interpretation
  -> apply_command / create_batch
  -> create_event, update_event, delete_event, or undo_last
  -> Event + Audit in the same database transaction
  -> durable reply/outbox state
  -> deliver after commit
```

`events.py` is the authoritative journal mutation boundary. `create_batch` adds atomic linked
drafts but delegates every fact to `create_event`. API and MCP also call the same event
operations. A transport retry reuses the persisted update/idempotency key and must not create
a second fact. An uncertain send remains uncertain and is not blindly retried.

A proactive question follows:

```text
generate_questions (evidence + eligibility)
  -> PendingQuestion with a stable dedup key
  -> select/reserve under the shared policy and budget
  -> durable outbox state
  -> deliver after commit
  -> sent or uncertain status from the observed transport outcome
```

Edits, `/cancel`, `/forget_conversation`, and undo preserve revision/epoch fences. A late
provider or transport result cannot recreate explicitly forgotten context.

## First vertical scenario plan: `user.focus_session`

The example is deliberately non-medical: an interval, concentration on an ordinal 1–5
scale, distraction count, and an optional note. UNI-01 only fixes its acceptance plan.

1. UNI-03 registers the definition and immutable schema version as data. There must be no
   `FocusSession` Python class, type branch, static keyboard entry, prompt constant, or SQL
   column for this tracker.
2. UNI-04 maps duration, concentration, and distraction count to versioned metric contracts;
   ordinal concentration is not treated as a physical gauge.
3. UNI-07 generates setup and entry forms from the active definition, supports correction by
   revision, and exports the definition without personal facts.
4. UNI-09/10 carry the same command and result through neutral message contracts.
5. UNI-11 renders it in Telegram; UNI-12 executes the same use-case suite through a restrictive
   text-only fake adapter without domain changes.
6. UNI-14 provides bounded history and distribution queries. LLM availability is not required.

The completed vertical slice must create two entries, correct one, preserve IDs/audit/time,
return history and a permitted distribution, and export only the reusable definition. Until
those later tasks land, the project must not claim that custom trackers are implemented.
