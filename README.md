# Garmin AI

Private Garmin history, Telegram diary, and personal analytics. PostgreSQL/TimescaleDB
holds the history; Gemini interprets messages and chooses bounded analysis tools.
Queries continue working when Garmin is unavailable.

## Implemented

- Automatic Garmin synchronization, historical reconciliation, immutable raw JSON/FIT archives,
  daily summaries, intraday measurements, activity details and FIT samples.
- A private Telegram bot for caffeine, migraine, medication and context entries, corrections,
  undo, questions, voice transcription, and evidence-based follow-ups.
- Personal baselines, period comparisons with uncertainty, activity efficiency, event windows,
  migraine/control comparisons and exploratory lagged associations.
- An authenticated local HTTP API, local MCP tools, encrypted backups, restore/export/erasure,
  persistent jobs, operational metrics, and container deployment.

See [verification and limits](docs/verification.md), [coverage](docs/garmin-endpoint-matrix.md),
[operations](docs/operations.md) and [analysis methods](docs/analysis-methods.md).
A populated response from Garmin is not proof of complete daily coverage. Missing values remain missing.

## Setup

Requirements: Linux, macOS or WSL2 with Docker Compose, Python 3.13 and `uv`. Native Windows deployment setup is unsupported. Use a private, backed-up local disk.

```sh
uv sync --locked
uv run python scripts/configure.py
docker compose up -d --wait db
uv run garmin-ai login
# Add GA_TELEGRAM_BOT_TOKEN, GA_TELEGRAM_USER_ID and GA_GEMINI_API_KEY to .env.
# Set an API-accessible GA_GEMINI_MODEL and GA_LLM_ENABLED=true.
docker compose up -d --build
```

The setup script preserves existing settings and generates local database, API and backup keys.
The backup key must also be saved separately in a password manager; losing it prevents recovery.
A Gemini consumer subscription is separate from API access and quota. No model ID is embedded
in the business logic. Inspect the models available to the configured API project.

`GA_TELEGRAM_USER_ID` is the numeric ID of the sole owner. The bot accepts only that owner's
private chat; zero leaves polling disabled. Garmin login asks for email, password and MFA locally.
Never send passwords, MFA codes, bot tokens or API keys in chat or commit them.

## Everyday Telegram use

Examples: «кофе был в 11», «мигрень началась часа два назад, 6 из 10»,
«закончилась в 18:30», «исправь силу боли на 4», «как изменился мой сон за месяц?».
Unknown medication names/doses and ambiguous times require clarification.
Voice is interpreted through the same validated diary flow.

`/today`, `/history`, `/status`, `/undo`, `/cancel`, `/pause`, `/resume` work without asking for credentials.
The inline buttons record simple facts. Follow-up questions use dated text and bounded frequency.
`/pause` stops proactive messages while synchronization continues.

## Local interfaces

The API binds to `127.0.0.1:8080`. `/health/live` and `/health/ready` expose only readiness;
`/tools`, `/tools/{name}`, `/events`, `/metrics` and `/operations` require `Bearer GA_API_KEY`.
No public ingress is configured. Put authentication and TLS in front of any remote deployment.

Start MCP with `uv run garmin-ai mcp`. It reads the local database, never Garmin directly.
The three event-write tools are explicitly annotated; updates use patches and revision checks.
See the project-scoped Codex configuration example in the operations guide.

## Development

```sh
uv run ruff check .
uv run ruff format --check .
# GA_TEST_DATABASE_URL must point to a disposable database whose name ends in _test.
uv run pytest -q
```

Database tests skip if a dedicated test database is absent. CI provisions a real TimescaleDB.
Live Gemini tests require `GA_LIVE_GEMINI_TESTS=1`, use synthetic facts, and consume API quota.
[Contribution rules](CONTRIBUTING.md) describe the non-draft PR and 30-minute review process.
