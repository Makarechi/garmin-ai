# Community extension stage: model lifecycle

Baseline for this stage: `8d9bd0208da5891f836ce7045004ef729fa40aab`.
This page describes the change introduced by this branch, not a complete public SDK
or a supported release. The six source documents in `docs/garmin-ai-community-plan/`
are design context.

## Completion checks

An external package is discovered only when its `kind.provider` entry point matches
an explicitly enabled integration instance. The runtime and the tracker HTTP route
create the selected model through the registry; the application service validates
its output before writing. The fixture's factory and handler are exercised, a
repeated HTTP operation writes one fact, and a second instance keeps its own config,
secret references, call count, and lifecycle. Empty/disabled selection and missing
packages leave the core diary available. Existing consent, sharing, and core-only
regression checks still run.

## Current public capabilities and remaining branches

| Area | Present in the baseline | This stage | Still to migrate or verify |
| --- | --- | --- | --- |
| Local diary, custom trackers, HTTP API | Implemented and exercised with synthetic DB tests | Unchanged | Browser demo loop and release installation |
| Garmin source | Optional SDK, sync jobs and local archive | Legacy settings remain mapped to stable instance ID | Source factory, job scheduling, cursor and per-instance state still have Garmin-specific branches |
| Telegram channel | Optional SDK, private bot, queue/outbox | Existing selection and delivery flow retained | Startup, ingress and delivery still have Telegram-specific branches |
| Gemini model | Optional SDK, consent and cooldown gate | Runtime and tracker HTTP route resolve via registry | Other provider-specific code and live provider behavior remain |
| External model extension | Registry shape existed but did not drive runtime | Entry-point discovery, explicit enablement, validated config and named secret references; synthetic fixture | General model capability kit, network provider gates, audio, contract stability and live verification |

`IntegrationInstance` now accepts `config` (non-secret scalar values) and
`secret_refs` (names of `GA_PLUGIN_*` environment variables). The registry passes
only that instance's validated config and named secret values to an external
factory. The fixture uses `model` and `response` in its validated config. A model
extension has `structured(...)`, optional `transcribe(...)` when it advertises
transcription, and `close()`. The contract version is `1` for this experimental
slice. There is no compatibility/deprecation promise yet.

For Compose deployment, put referenced `GA_PLUGIN_*` values in a local
`.plugin.env` file readable only by the owner, or set `GA_PLUGIN_ENV_FILE` to
another private env-file path. Compose injects this optional file into both the
API and worker containers; it does not pass the worker's entire `.env` to the
API. The `.plugin.env` path is ignored by Git. Plugin code must be included in
the built application image separately; the synthetic fixture test installs it
only in the test environment. A model plugin must advertise
`structured_output`; transcription alone cannot serve the text lifecycle.

External Python extensions run inside the application process and are trusted code.
The restricted factory argument is an interface boundary, not a security sandbox.
Only install code you trust. Installation alone does not enable an integration.
The owner must also set `GA_LLM_ENABLED=true`, select exactly one model instance,
and grant model consent for that provider, instance ID, model, and data categories.
The old `GA_*` Gemini, Garmin and Telegram settings still resolve their stable
default instances when `GA_INTEGRATIONS` is absent. `GA_INTEGRATIONS=[]` disables
that legacy discovery.

## Reproduce without provider accounts

```sh
uv sync --locked
uv pip install --python .venv/bin/python --no-deps -e examples/synthetic-model
.venv/bin/pytest -q \
  tests/test_synthetic_plugin.py::test_explicit_discovery_and_agent_handler \
  tests/test_synthetic_plugin.py::test_instance_configuration_secret_scope_and_consent
```

For the HTTP and core diary checks, start a **disposable** PostgreSQL/TimescaleDB
database named with `_test`, set `GA_TEST_DATABASE_URL` to its connection URL, then:

```sh
.venv/bin/pytest -q tests/test_synthetic_plugin.py tests/test_core_only_flow.py
```

`tests/test_synthetic_plugin.py` tests the installed package with no Garmin,
Telegram, or Gemini SDK. CI installs the fixture after the general suite and runs
the same contract test. The fixture has no network client and uses only invented
records. The HTTP test checks the real request path and database state; it is not
a browser or live-provider test.

This stage changes no database schema. Existing explicit integration lists remain
valid. A configuration with more than one enabled model now fails rather than
silently choosing the first. Source/channel migration, a complete SDK, a live
second backend, the interactive demo, release packaging, and operational acceptance
remain separate work in CP-02 through CP-11. No live provider call, seven-day run,
or off-host restore is claimed here.
