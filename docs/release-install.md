# Docker installation candidate

This is a **candidate bundle**, not a published or supported release. It contains a
standalone Compose file, local setup helper, static synthetic demo and a manifest.
The application image must be supplied as an immutable registry digest. No Python,
`uv`, Git checkout or source tree is needed on the installation host. Docker with
Compose is required. The database image is pinned in the bundle as well.

## Build and inspect a candidate

From a clean source revision, after building and checking an application image,
replace the placeholder below with its actual registry digest:

```sh
uv run python scripts/build_release_bundle.py \
  --app-image ghcr.io/OWNER/garmin-ai@sha256:FULL_64_CHARACTER_DIGEST
```

The builder rejects uncommitted changes, images without a full digest, and a
release Compose file that has drifted from the development service layout. It
produces a `.tar.gz` and a SHA-256 checksum in `dist/`. The archive contains no
credentials, health records or original data. The manifest records the exact Git
SHA, application and database image digests, schema revision and candidate
status. Its empty `validated_platforms` list is intentional until each platform
is tested against the exact image digest. The checksum does not authenticate an
untrusted download source.

Before public distribution, collect third-party license texts and required
notices for the final image and database image, test the actual target platforms,
complete the [operational acceptance](operational-acceptance.md), and update the
manifest only from that evidence. This bundle alone is not release clearance.

## Use the bundle on a private host

Extract the archive into a dedicated private directory. Review
`release-manifest.json` and `release.env` before running anything. Keep that
directory private: setup writes `.env` with keys there.

```sh
./install.sh demo
# Open http://127.0.0.1:8765/dashboard and use fictional data only. Stop with Ctrl-C.
./install.sh setup --locale ru --timezone Europe/Bratislava --units metric
./install.sh start
./install.sh status
```

`setup` runs the bundled configuration helper inside the digest-pinned image,
generates local database, API and backup keys, and creates private storage
directories. It does not start Garmin, Telegram or a model. Re-running it keeps
existing keys and records. Save the backup key separately in a password manager;
without it encrypted backups cannot be restored. `start` runs the bundled schema
migration before API and worker startup, then waits for health checks. The API and
database ports bind to loopback. The demo runs without a database or account and
keeps fictional edits in browser memory only.

The local diary and custom tracker forms work without optional integrations.
If you later enable Garmin, Telegram, or a model, follow the respective guides
and the [provider consent](provider-consent.md). Credentials must be entered on
the private host, never in a public chat or repository. Garmin MFA may require
owner action. A custom Python plugin requires a separately built trusted image;
it is not silently installed by this bundle.

## Update safely

The archive does not perform an unattended update or rollback. Before moving to
a new bundle, record the current image digest, manifest SHA, schema revision and
plugin versions. Stop the **worker** while keeping the database running. Create
and verify an encrypted backup using the current image and the project's
[backup instructions](operations.md#backup-and-restore). Keep a copy outside the
host, with the key stored separately. Do not copy plaintext recovery files into
the new bundle.

Copy the existing `.env` to the new private bundle directory with owner-only
permissions. Its absolute storage paths and Compose project name must stay the
same. Inspect the new manifest and plugin compatibility, then run `./install.sh
setup` to validate preserved settings and `./install.sh start` to apply only the
forward schema migration. Check `./install.sh status`, API readiness, worker
heartbeat, source freshness, queue errors and backup age. If the new version is
incompatible, stop it and restore the verified backup to an isolated target or
use an older binary only when its documented schema compatibility allows it.
Never run a down migration or force the migration ledger to make an old image
start.

The application image, platform matrix, third-party notices, seven-day run and
independent-host restore are still pending before a supported public release.
