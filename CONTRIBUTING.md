# Contributing

Garmin AI is a self-hosted personal diary and analytics project. You can contribute
without a Garmin account, Telegram bot, model API key, or access to anyone's health
data. The project's own code is available under [Apache-2.0](LICENSE). Third-party
dependencies and container images retain their own licenses.

## First local check

Install Python 3.13 and [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q tests/test_architecture_boundaries.py tests/test_integrations.py
```

For a complete diary write/correction test, use a disposable PostgreSQL/TimescaleDB
database whose name ends in `_test`, set `GA_TEST_DATABASE_URL`, and run
`uv run pytest -q tests/test_core_only_flow.py`. The test suite refuses a database
without that suffix. No provider SDK or real account is needed. Do not put a real
credential in a test command or fixture.

## Where to work

| Area | Main paths |
| --- | --- |
| Diary, definitions, forms | `src/garmin_ai/events.py`, `definitions.py`, `tracker_forms.py` |
| Read and analysis | `queries.py`, `generic_analytics.py`, `agent.py` |
| Sources and ingestion | `garmin.py`, `ingest.py`, `sync.py` |
| Channels and models | `channels.py`, `telegram_adapter.py`, `llm.py` |
| Integration selection | `config.py`, `integrations.py`, `runtime.py` |
| HTTP and dashboard | `api.py`, `dashboard.py`, `static/dashboard/` |
| Tests and architecture | `tests/`, `docs/architecture.md` |

Four useful kinds of contribution are: a declarative tracker pack, translation, or
example; a source adapter or importer; a channel or model adapter; and a regression
fix, usability improvement, or documentation correction. Start with a small issue
and one focused pull request. The [extension stage guide](docs/community-extension-stage.md)
describes the current runtime boundary. The [extension contract and test kit](docs/extension-sdk.md)
has runnable source, channel and model fixtures and states which paths are still
experimental. Do not assume an installed package is automatically enabled.

Open a PR against `main` with the problem, what changed, and the exact checks you ran.
Include a synthetic example when behavior changes. Maintainers review and merge PRs;
contributors do not need paid tools or permission to request a review. The project's
maintainer workflow is in `AGENTS.md`, not a requirement for external contributors.

Only synthetic or explicitly redacted data belongs in Git. Never attach Garmin tokens,
raw health exports, FIT files, original voice recordings, database dumps, backup keys,
or unredacted logs to an issue or PR. Inspect the staged diff before committing.
Report a suspected vulnerability privately using [the security instructions](SECURITY.md).
