# GCP single-host deployment

This deployment uses the application from `feat/deployment-validation` on one
Debian 12 `e2-micro` VM in `us-central1-a`, with a 30 GB `pd-standard` boot disk.
The VM does not sleep when idle. Public IPv4 and usage outside Free Tier limits
can incur charges. The Free Tier allowance is shared across the billing account.

Build the x86 image on a development machine, never on the 1 GB VM:

```sh
docker build --platform linux/amd64 -t garmin-ai:gcp .
docker save garmin-ai:gcp | gzip > garmin-ai-image.tar.gz
```

`deploy/gcp/bootstrap.sh` installs Docker and creates a 2 GB swap file. It must
run as root on a dedicated Debian 12 x86 VM. SSH is restricted to Google's IAP
range; the API and database remain bound to localhost. Use an SSH tunnel for API
access. No public application or database firewall rules are required.

Deploy `compose.yml`, `compose.gcp.yml`, and `deploy/gcp/healthcheck.py` under
`/opt/garmin-ai`, preserving their relative paths, and securely
transfer the private `.env`, data, Garmin tokens, and a consistent database export.
Use UID/GID 1000 and private, non-overlapping directories under `/opt/garmin-ai`.
Load the saved image, then use:

```sh
docker compose -f compose.yml -f compose.gcp.yml up -d --wait db
docker compose -f compose.yml -f compose.gcp.yml run --rm --no-deps migrate
# Restore the database and private files before enabling the worker.
docker compose -f compose.yml -f compose.gcp.yml up -d --no-build --wait
```

Stop the old worker and API before taking the final export. Preserve the old
installation as a rollback copy, with its worker stopped: two Telegram pollers
must never run against separate copies of the database.

The image precompiles Python bytecode during the build to avoid compilation on
the small VM. Health checks have a five-minute cold-start grace period. The
worker probe reads its committed database heartbeat directly to avoid repeatedly
importing the full application on the shared CPU.

The override disables automatic database tuning, limits database memory and
parallel queries, uses one numerical-library thread per process, and rotates
container logs. Swap is a fallback for peaks, not a substitute for RAM. Check
memory, swap, OOM counters, readiness, backup creation, and restored row counts.
Reboot the VM and repeat health and persistence checks before declaring success.
Keep an encrypted backup outside the VM; on-disk daily backups share its failure
domain. Never commit deployment archives, credentials, tokens, or health data.
