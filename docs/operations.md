# Operations and recovery

## Start and inspect

`docker compose up -d --build` builds the private local image, waits for PostgreSQL, runs migrations,
and starts the API and worker. `docker compose ps` should show healthy services. The API and database
ports bind only to loopback. Docker must be running; laptop sleep stops useful background execution.
For continuous operation use an always-on machine with the same Compose configuration.

`docker compose logs --tail 50 worker` emits job IDs, job types and safe error categories.
It does not log diary text, health values, request URLs, bot tokens or Gemini prompts.
Authenticated `/operations` and `/metrics` expose job counts, source processing states, heartbeat
age and last successful backup age. Missing age is `-1` in the metrics format. Readiness validates
the expected migration revision, not just a database connection.

`docker compose restart worker` verifies recovery without discarding queued work. A PostgreSQL
advisory lock permits one runtime. API and MCP may run alongside it. Leases expire and bounded
retries recover abandoned jobs. Long-running custom workers must renew with their original lease duration.

## Garmin authentication and historical data

Stop the worker with `docker compose stop worker`, then use `uv run garmin-ai login` on the host.
It asks for email/password/MFA in the terminal and writes
restricted token files under `tokens/garmin`. Passwords are not stored by this application.
The container mounts that directory. Run `docker compose up -d worker` after login; the next retry
loads refreshed credentials after an auth failure.
The bot sends a deduplicated re-authentication alert if Garmin requires a new login.

For a bounded historical investigation:

```sh
uv run garmin-ai probe --start 2026-08-25 --end 2026-09-07
uv run garmin-ai import-probe
```

The probe is limited to 31 dates and a sample of five activity detail/FIT exports. Larger ranges
can be processed in non-overlapping batches. Raw archive files and `coverage-report.json` stay
private. Replays retain request timestamps; older data cannot overwrite newer activity/FIT versions.
Normal operation reconciles the previous seven days nightly and 30 days on Mondays.

## Telegram and Gemini

Configure the owner ID and bot token in `.env` before starting the worker. Leave
`GA_GEMINI_THINKING_LEVEL` empty unless the selected model supports that option. The bot rejects groups
and other senders. `/pause` and `/resume` control proactive messages; ordinary diary commands keep
working. The default question budget is two per local day, with quiet hours 22:00–08:00 and a
category cooldown. Already answered, ambiguous-delivery or expired prompts are not repeatedly sent.

Gemini requires a model available to the API key's project and sufficient API quota. Raw storage
and deterministic tools keep working during quota failures. Text/voice parsing waits for provider
availability; the owner receives a concise quota notice. Provider requests use `store=false`,
but this does not replace the provider's own data-processing or retention terms. Only necessary
bounded context is sent; full raw archives and credentials are not included in prompts.

## Backup and restore

The worker creates an encrypted daily backup in `GA_BACKUP_DIR` (default `backups`, outside the source data directory). It contains a consistent
PostgreSQL snapshot, raw archive files and Garmin tokens. Encryption uses AES-256-GCM with a fresh
nonce and authenticated header. Restore verifies the authentication tag before unpacking.
The key in `GA_BACKUP_KEY` encodes 32 random bytes and must be kept separately from the backups.

```sh
docker compose stop worker
uv run garmin-ai backup /path/to/backup.enc
docker compose up -d worker
uv run garmin-ai unpack-backup /path/to/backup.enc /path/to/new-recovery-directory
```

Manual backup holds the same file and database coordination locks as login/probe, so the worker
must be stopped during the snapshot. Data and token roots must not overlap.
The encrypted destination may be separate media. Plaintext staging stays in the private local
`data/backup-work` area beside the original data; use trusted storage for this data directory.
A crash can leave staging files there. Backups are not automatically sent to another device:
copy encrypted files to your chosen off-device storage. Local backups alone do not protect against
loss of the whole machine. The most recent 14 scheduled daily copies are retained by default (`GA_BACKUP_KEEP_DAILY`). Manual backup filenames are not pruned.

`unpack-backup` requires the backup key but does not require a database connection. It refuses
an existing destination directory and rejects unsafe archive members. The recovered layout is
`database.jsonl.gz`, `coverage-report.json` when present, `raw/`, and `tokens/`.

To restore, provision an **empty** destination PostgreSQL database, point `GA_DATABASE_URL` at it,
run `uv run garmin-ai migrate`, then:

```sh
uv run garmin-ai restore-db /path/to/new-recovery-directory/database.jsonl.gz
```

Copy the recovered coverage report into the data directory if it exists. Copy recovered `raw/` into the configured data directory and `tokens/` into the token directory,
retaining private permissions. Start services only after paths and credentials are configured.
Restore is transactional and refuses a nonempty destination. Keep the old database intact until
counts and representative queries in the restored database have been verified.

## Export and deletion

`uv run garmin-ai export /private/path/export.jsonl.gz` creates a portable **unencrypted** JSON-lines
export of the database, including audit and operational records. Permissions are restricted. Raw
files are included in encrypted backups, not this database-only export.

API/MCP event deletion is reversible and audited. To permanently erase **all local** data, first
stop the API and worker and close MCP clients. Run this command only with the intended local paths:

```sh
docker compose stop api worker
uv run garmin-ai erase-all --confirm 'ERASE ALL LOCAL HEALTH DATA'
```

Erasure removes database health/history rows, the configured data directory and Garmin tokens. The separate backup directory is retained. A persistent maintenance marker rejects late API/MCP writes.
It does not erase separate backup copies, Telegram messages or provider-side records. After explicit
new setup, `uv run garmin-ai resume-storage` re-enables the empty local store.

## Codex MCP configuration

Create a local `.codex/config.toml` in this trusted project; adjust absolute paths:

```toml
[mcp_servers.garmin-ai]
command = "/absolute/path/to/uv"
args = ["run", "--locked", "garmin-ai", "mcp"]
cwd = "/absolute/path/to/garmin-ai"
startup_timeout_sec = 30
tool_timeout_sec = 60
```

`codex mcp get garmin-ai` verifies the configuration. A new/reloaded Codex connection loads it.
The server reads the project's `.env` locally; no secrets belong in MCP arguments. Its read tools
query PostgreSQL only. Writes use explicit patches, stable creation keys, revision checks and audit.
[Official Codex MCP configuration](https://developers.openai.com/codex/mcp) documents project scope.

## Recovery checks

- API readiness 200, unauthenticated tools 401, authenticated tools/metrics 200.
- Worker heartbeat below three minutes and queued current-day jobs progressing.
- A private backup decrypts and restores into a disposable empty database with matching records.
- Restart preserves data, leases and Telegram deduplication; do not run two pollers.
- Inspect unresolved jobs and freshness per source day; historical reconciliation is not current-day freshness.

The one-week unattended acceptance window starts after stable deployment. Do not claim that it
passed until seven days of observation actually exist.


Standalone `garmin-ai login` and `garmin-ai probe` require a stopped worker and
hold a local file lock for the entire operation. Probe works without a database URL;
when configured, PostgreSQL also coordinates with container workers, even before migration.
`GA_LOCK_DIR` (default `.state`) must be outside the data and token directories and shared
by every process using those files. Compose mounts this directory separately. Erasure retains
only coordination metadata there and blocks ingestion until an explicit restore or `resume-storage`.
A deliberate login after erasure can save new tokens, but does not resume ingestion.
An erased database may be restored directly: the database erasure marker is removed transactionally
only when the complete restore succeeds; the CLI also clears the local marker after success.
Configure `GA_BACKUP_DIR` on a separate disk or mounted backup volume for protection against
source-filesystem loss. The default sibling directory only isolates backups from source erasure.
