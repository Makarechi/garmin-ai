# Garmin AI

Private Garmin history, Telegram diary, and personal analytics. PostgreSQL/TimescaleDB
holds the history; Gemini interprets messages and chooses bounded analysis tools.
Queries continue working when Garmin is unavailable.

## Implemented

- Installation-local owner identity, selectable scenario packs, versioned event and metric
  definitions, and user-created trackers that do not require Python or database schema changes.
- Generated forms, bounded natural-language tracker setup, tracker-driven check-ins, generic
  analytics, localized onboarding, and transport-neutral messaging with a restricted text-only
  reference channel.
- Automatic Garmin synchronization, historical reconciliation, immutable raw JSON/FIT archives,
  daily summaries, intraday measurements, activity details and FIT samples.
- A private Telegram bot for caffeine, migraine, medication and context entries, corrections,
  undo, questions, voice transcription, and evidence-based follow-ups.
- Personal baselines, period comparisons with uncertainty, activity efficiency, event windows,
  migraine/control comparisons and exploratory lagged associations.
- An authenticated local HTTP API and web chat, local MCP tools, encrypted backups, restore/export/erasure,
  persistent jobs, operational metrics, and container deployment.

See the [public capability matrix and roadmap](docs/public-capabilities.md) for
the evidence and limits of the main diary, integration and release paths. Also see [universal acceptance](docs/universal-acceptance.md),
[verification and limits](docs/verification.md), [coverage](docs/garmin-endpoint-matrix.md),
[operations](docs/operations.md) and [analysis methods](docs/analysis-methods.md).
A populated response from Garmin is not proof of complete daily coverage. Missing values remain missing.

## Try the interactive demo

Run `python3 scripts/serve_demo.py` and open `http://127.0.0.1:8765/dashboard`.
No Garmin account, bot, model, database or project installation is needed. Create a tracker,
record and correct a fictional fact, and see the recalculated summary. The [demo guide](docs/interactive-demo.md)
explains the local-only data and reset behavior. The authenticated dashboard on a configured
instance also offers this demo before connecting personal data.

For a configured instance, see [dashboard setup](docs/data-dashboard.md). To build
without external accounts, follow [Contributing](CONTRIBUTING.md). The
[community extension stage](docs/community-extension-stage.md) describes the
experimental model plugin path and its limits. Garmin, Telegram, and Gemini are
optional integrations, not requirements for the local diary. This project is not
affiliated with or endorsed by Garmin.

The project's code is licensed under [Apache-2.0](LICENSE). Dependencies and
container images retain their own licenses; see the [distribution review](docs/license-review.md).

## Install

For a Docker-only installation candidate that needs no Python or source checkout
on the installation host, see the [release installer](docs/release-install.md).
It has local smoke evidence but is not yet a supported public release.

### Setup from source

Requirements: Linux, macOS or WSL2 with Docker Compose, Python 3.13 and `uv`. Native Windows deployment setup is unsupported. Use a private, backed-up local disk.

```sh
uv sync --locked
uv run python scripts/configure.py
docker compose up -d --wait db
uv run garmin-ai migrate
docker compose up -d --build
```

This starts the local diary without external accounts. To connect Garmin,
Telegram or Gemini later, install the relevant optional dependencies and follow
the [operations guide](docs/operations.md), [Telegram pairing guide](docs/telegram-pairing.md)
and [provider consent guide](docs/provider-consent.md). Enter credentials only
on the private host. Text model processing needs health and diary consent; voice
also needs audio consent.

The [Telegram pairing guide](docs/telegram-pairing.md) covers separate environment files and existing installations.
An optional [Telegram reauthentication form](docs/garmin-telegram-reauth.md) can restore expired Garmin tokens using an email code and a password kept in Google Secret Manager.

On Linux with enforcing SELinux, complete the [host labeling step](docs/operations.md#selinux-host-preparation) after configuration and before starting the worker.

For independent owners on one Docker host, configure separate clones with
[distinct instance names and ports](docs/instance-configuration.md).

The setup script preserves existing settings and generates local database, API and backup keys.
The backup key must also be saved separately in a password manager; losing it prevents recovery.
External model processing defaults to disabled until you record the explicit
[provider consent](docs/provider-consent.md) for your configured model and data categories.
A Gemini consumer subscription is separate from API access and quota. No model ID is embedded
in the business logic. Inspect the models available to the configured API project.

The database creates its own opaque owner ID and profile before Garmin or Telegram is connected.
`GA_TELEGRAM_USER_ID` is the numeric Telegram identity explicitly paired to that owner. The bot
accepts only that owner's private chat; zero leaves polling disabled. Garmin login asks for email,
password and MFA locally. External account IDs never replace the internal owner identity.
Never send passwords, MFA codes, bot tokens or API keys in chat or commit them.

Garmin, Telegram and model-provider packages are optional. A local tracker-only installation can
complete onboarding without connecting any external service. Use `--extra full`
only when those integrations are needed.

## Everyday Telegram use

Examples: «кофе был в 11», «мигрень началась часа два назад, 6 из 10»,
«закончилась в 18:30», «исправь силу боли на 4», «как изменился мой сон за месяц?».
Explicitly reported medication intake may retain unknown name/dose/unit as null; ambiguous intake or time requires clarification.
Voice is interpreted through the same validated diary flow.

`/today`, `/history`, `/status`, `/undo`, `/cancel`, `/pause`, `/resume` work without asking for credentials.
The inline buttons record simple facts. Follow-up questions use dated text and bounded frequency.
`/pause` stops proactive messages while synchronization continues.

## Local interfaces

The API binds to `127.0.0.1:8080`. `/health/live` and `/health/ready` expose only readiness;
`/tools`, `/tools/{name}`, `/events`, `/metrics` and `/operations` require `Bearer GA_API_KEY`.
The [local web chat](docs/local-web-chat.md) is at `/chat` and requires the owner API key.
No public ingress is configured. Put authentication and TLS in front of any remote deployment.

Start MCP with `uv run garmin-ai mcp`. It reads the local database, never Garmin directly.
MCP is read-only by default. Set `GA_MCP_ENABLE_WRITES=true` locally to enable audited diary writes.
See the project-scoped Codex configuration example in the operations guide.

## Development

```sh
uv run playwright install chromium  # required once for the browser demo test
uv run ruff check .
uv run ruff format --check .
# GA_TEST_DATABASE_URL must point to a disposable database whose name ends in _test.
uv run pytest -q
```

Database tests skip if a dedicated test database is absent. CI provisions a real TimescaleDB.
Live Gemini tests require `GA_LIVE_GEMINI_TESTS=1`, use synthetic facts, and consume API quota.
[Contribution rules](CONTRIBUTING.md) describe the non-draft PR and 30-minute review process.
