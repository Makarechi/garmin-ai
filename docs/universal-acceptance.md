# Universal tracker acceptance

The universal release gate is backed by
[`tests/universal_acceptance_manifest.json`](../tests/universal_acceptance_manifest.json). It maps
every scenario from `UT-01` through `UT-53` to one or more test node IDs. A release-gate test
checks that the range is complete and that every referenced test still exists. The normal CI job
runs those tests against a disposable PostgreSQL/TimescaleDB database and preserves JUnit,
coverage and release metadata as build artifacts.

The exact commit SHA is intentionally captured at build time rather than copied into this file.
`scripts/release_metadata.py` records the SHA, database revision, commands, environment boundary
and whether live services were used. The current database revision is `c8f51d3a7e20`.

## What the gate proves

- Legacy diary operations, owner separation, definitions, versioned metrics, migrations, packs,
  generated forms and natural-language proposals retain their tested contracts (`UT-01`–`UT-25`).
- Neutral channel identity, capabilities, receipts, idempotency, conversations, Telegram parity
  and restricted text-only operation are covered (`UT-26`–`UT-40`).
- `UT-39` now cites both the original service-level `submit_form` test and actual HTTP, Telegram,
  and restricted-text ingress tests for create, edit, close, and invalid-value clarification.
  `UT-40` adds a persisted reference-channel restart test for text fallback, voice fallback,
  and stale revision rejection. These synthetic routes do not establish live provider behavior.
- Tracker-driven check-ins, restart revalidation, shared policy, generic analytics, onboarding,
  safe sharing, permissions and sensitive-data consent are covered (`UT-41`–`UT-53`).
- Portable export/restore and leased-job recovery are automated. Release rollback means restoring
  the verified pre-release backup or moving forward with the compatible reader; running an old
  binary against a database containing new custom facts is deliberately unsupported.

The `core-only` CI job installs no Garmin, Telegram or model SDK extras and checks the CLI,
runtime, architecture boundaries and release manifest. The full job installs all extras and runs
the complete legacy and universal test suite.

## Verification boundary

All committed fixtures are synthetic. CI does not authenticate to Garmin, send Telegram messages,
call a model provider, use personal health data, or claim WhatsApp support. Provider-specific live
checks require a separate explicit decision and their results must be reported separately. A unit
or schema-validation result is not described as a production or live result.

No acceptance scenario is conditionally skipped by the manifest. Any skip produced by the full
suite remains visible in the preserved JUnit output and `pytest -ra` summary; the known live/model
and host-filesystem checks explain their opt-in or platform reason at the test itself.
