# Native container build

`docker buildx build --load --platform linux/arm64 -f Dockerfile.rust -t garmin-ai-rust:local .` builds the native image. The same command accepts `linux/amd64`. The image starts the HTTP service on port 8080 by default.

The runtime uses UID and GID 1000, with certificate and timezone data. It receives the existing `GA_*` settings at startup. The data, token, state and backup paths are under `/app`. The built-in probe checks HTTP liveness. `garmin-ai-rust healthcheck --ready` also requires an accessible database at the current schema.

Both image architectures passed the core-only probe on a read-only filesystem with Linux capabilities dropped and networking disabled. A separate run installed an empty TimescaleDB, upgraded a database at `f103aa712b44`, confirmed `c54f8a72d130`, repeated migration, and checked HTTP readiness. The local ARM64 Docker VM ran AMD64 through emulation.

## Local configuration

Run `garmin-ai-rust configure` from the installation directory to prepare
`.env` and private data, token, backup and lock directories. Use
`--instance`, `--db-port` and `--api-port` when creating an additional
installation. The command keeps existing secrets and initialized instance
identity. It checks the configured paths and database endpoints before
replacing the settings file. It also accepts `--locale`, `--timezone` and
`--units`.

## Native Compose preview

`compose.rust.yml` runs PostgreSQL, the native migrator, the HTTP service, and the native worker.
Use a separate Compose project with its own database and storage. Set `GA_APP_IMAGE`,
`GA_POSTGRES_PASSWORD`, and `GA_CONTAINER_DATABASE_URL` for that project. The database host within
the Compose network is `db` and the database name is `garmin_ai`. Use the existing
`postgresql+psycopg://` URL form. `GA_API_PORT` controls the loopback port on the host.
Set `GA_INTEGRATIONS` and `GA_TELEGRAM_USER_ID` in the Compose project environment so the API
and worker use the same bot selection. `GA_WORKER_ENV_FILE` can supply worker-only settings.

`docker compose -f compose.rust.yml up --detach --wait` waits for PostgreSQL and migration before
starting the application. HTTP readiness and worker health use their native probes.
`python3 migration/check_native_compose.py IMAGE --platform linux/arm64` exercises a fresh
installation in disposable volumes. Pass a pinned Python builder image with `--legacy-image`
to exercise the upgrade from `f103aa712b44`.
