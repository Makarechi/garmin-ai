# GCP single-host deployment

## Native release bundle

Use a verified Linux AMD64 [installation bundle](release-install.md) on the
dedicated Debian host. Extract it as the deployment user. Bootstrap a fresh host
as root, then configure and start the release as the deployment user:

```sh
sudo ./deploy/gcp/bootstrap.sh --rust
./install.sh --gcp setup
./install.sh --gcp start
./install.sh --gcp status
```

The GCP profile applies the small-host memory limits and bounded Docker logs.
The host needs Docker, Compose, and the standard Debian shell tools.
The application and worker run their own readiness checks. Setup, demo, backup,
recovery, and host retention use the binaries packaged with the release.

When upgrading, keep the existing Compose project name and storage paths.
Follow the [backup and upgrade procedure](release-install.md#update-safely)
before starting the replacement. Run the same `--gcp` form for `stop`,
`backup`, and `unpack-backup`. Keep the prior release available for rollback.
An already bootstrapped host can continue to use its existing Docker installation.

For the optional browser sign-in gateway, set these values in the private
`.env` before starting the profile. Substitute the hostname approved for Garmin
sign-in.

```dotenv
COMPOSE_PROFILES=garmin-web-auth
GA_GARMIN_AUTH_HOST=garmin.example.org
GA_GARMIN_AUTH_URL=https://garmin.example.org/garmin-auth
```

The gateway serves the sign-in path on HTTPS. The main API and database ports
remain on the loopback interface. Configure DNS and incoming gateway traffic
only when enabling this feature.

Once both application services are healthy, follow the
[native cleanup installation](release-cleanup.md#native-release-bundle).

## Legacy deployment

This deployment uses the application from `feat/deployment-validation` on one
Debian 12 `e2-micro` VM in `us-central1-a`, with a 30 GB `pd-standard` boot disk.
The VM does not sleep when idle. Public IPv4 and usage outside Free Tier limits
can incur charges. The Free Tier allowance is shared across the billing account.

Build the x86 image on a development machine, never on the 1 GB VM:

```sh
release_revision=$(git rev-parse --short=12 HEAD)
image="garmin-ai:gcp-$release_revision"
docker build --platform linux/amd64 -t "$image" .
docker save "$image" | gzip > "garmin-ai-gcp-$release_revision.tar.gz"
```

`deploy/gcp/bootstrap.sh` installs Docker and creates a 2 GB swap file. It must
run as root on a dedicated Debian 12 x86 VM. SSH is restricted to Google's IAP
range; the API and database remain bound to localhost. Use an SSH tunnel for API
access. No public application or database firewall rules are required.

Deploy `compose.yml`, `compose.gcp.yml`, and `deploy/gcp/healthcheck.py` under
`/opt/garmin-ai`, preserving their relative paths, and securely
transfer the private `.env`, data, Garmin tokens, and a consistent database export.
Use UID/GID 1000 and private, non-overlapping directories under `/opt/garmin-ai`.
Load the saved image and set `GA_IMAGE` in the private `.env` to the same unique
image tag, then use:

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

Install the host-side release cleanup described in [release-cleanup.md](release-cleanup.md).
It keeps the running application image and one verified rollback image, and removes
old Docker image upload archives daily. Database volumes, source data, credentials,
and backups are outside its cleanup scope. Recheck `df -h /` and `garmin-ai backup-space`
inside the configured worker after cleanup: retained release images can consume much
more space than the backups themselves on this 30 GB host.
