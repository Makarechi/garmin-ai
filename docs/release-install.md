# Docker installation candidate

This is a **candidate bundle**, not a published or supported release. It contains a
standalone Compose file, local setup helper, static synthetic demo and a manifest.
The application image must be supplied as an immutable registry digest. No Python,
`uv`, Git checkout or source tree is needed on the installation host. Bash and
Docker Compose 2.24.0 or newer are required. Run the installer as a dedicated
non-root account with Docker access. The database image is pinned in
the bundle as well.

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
To enable Garmin later, set its private values in `.env`, run `./install.sh login`
in the bundle directory, complete any MFA prompt in that terminal, and run
`./install.sh start`. For Telegram, set the bot token in `.env` and leave
`GA_TELEGRAM_USER_ID=0`; run `./install.sh pair-telegram`, send the printed
one-time code to the bot from your private Telegram chat, then run
`./install.sh start`. Both commands stop the worker before using its mounted
private storage; pairing writes the owner ID to the bundle's `.env`. Follow the
[Garmin guidance](operations.md#garmin-authentication-and-historical-data),
[Telegram pairing guide](telegram-pairing.md) and
[provider consent](provider-consent.md) for the full steps. Source-installation
commands in those guides use `uv`; use the bundle commands above on a host
without source or Python. Credentials must be entered on the private host,
never in a public chat or repository. A custom Python plugin requires a
separately built trusted image; it is not silently installed by this bundle.

## Update safely

The archive does not perform an unattended update or rollback. Before moving to
a new bundle, record the current image digest, manifest SHA, schema revision and
plugin versions. In the **old bundle directory**, with the database still
running, create and authenticate a backup:

```sh
./install.sh backup
```

This stops only the worker, runs the backup inside the pinned application image,
unpacks it to a temporary local Docker volume outside the backup directory to
verify its authenticated contents, removes that temporary copy and volume, and
leaves the worker stopped. The printed encrypted
filename is in `GA_BACKUP_DIR` from `.env`. Copy that file outside the host and
keep the backup key separately. Do not copy plaintext recovery files into the
new bundle. For a deliberate recovery inspection, run `./install.sh
unpack-backup FILENAME.enc`; it creates a private recovery directory inside
`GA_BACKUP_DIR`. Remove it securely after use. The general
[backup guidance](operations.md#backup-and-restore) still applies to data
handling, while its `uv` and bare Compose commands are for source installations.

Copy the existing `.env` to the new private bundle directory with owner-only
permissions. If the old bundle has `.plugin.env`, copy it with owner-only
permissions too. When `GA_PLUGIN_ENV_FILE` names a different relative file,
copy that file to the same relative path; when it names an absolute private
path, keep that path valid on the new bundle. Check that the plugin file is
present before starting: Compose treats its absence as optional and will not
report missing plugin credentials. Never put either file in the release archive.
The `.env` absolute storage paths and Compose project name must stay the
same. Inspect the new manifest and plugin compatibility, then run `./install.sh
setup` to validate preserved settings. It mounts the old storage paths at their
original absolute locations for this check. Run `./install.sh start` to apply only the
forward schema migration. Check `./install.sh status`, API readiness, worker
heartbeat, source freshness, queue errors and backup age. If the new version is
incompatible, stop it and restore the verified backup to an isolated target or
use an older binary only when its documented schema compatibility allows it.
Never run a down migration or force the migration ledger to make an old image
start.

The application image, platform matrix, third-party notices, seven-day run and
independent-host restore are still pending before a supported public release.
