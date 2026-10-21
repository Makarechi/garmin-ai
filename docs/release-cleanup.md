# GCP host release retention

The daily host timer retains the current `garmin-ai` image and one verified rollback
image. It runs at 12:00 UTC, with up to 15 minutes of jitter, at low CPU and I/O
priority. Missed runs are caught up after boot. It does not require the operator's
computer, a chat session, or the application scheduler to remain open.
If the host is still starting, a new deployment fails its health check, or another
safety check fails, the service retries after 30 minutes. It keeps both retained
images throughout partial progress and retries.

Before enabling it, confirm that both `api` and `worker` are healthy and identify a
distinct, previously working rollback image. Preview the exact selection on the VM:

```sh
sudo python3 deploy/gcp/release_cleanup.py \
  --initialize-rollback garmin-ai:PREVIOUS_VERIFIED_TAG \
  --archive-dir /home/UPLOAD_USER
```

Only after checking this preview, install from the same checked-out revision:

```sh
sudo sh deploy/gcp/install-cleanup.sh garmin-ai:PREVIOUS_VERIFIED_TAG /home/UPLOAD_USER
sudo systemctl start garmin-ai-release-cleanup.service
sudo journalctl -u garmin-ai-release-cleanup.service --no-pager -n 20
systemctl list-timers garmin-ai-release-cleanup.timer
```

Installation records the retained image identities without deleting anything, then
enables the persistent timer. The explicit service start above performs the first
cleanup. Installation is repeatable without replacing an existing rollback record.
The script requires only host Python 3 and the Docker CLI; it neither rebuilds the
application nor runs database migrations.

Upload future `docker save | gzip` installation files directly into the configured
upload directory, named `garmin-ai-RELEASE.tar.gz`. Give each release a unique Docker
image tag, such as `garmin-ai:gcp-GIT_SHA`, as in the GCP deployment instructions.
Do not overwrite a retained image tag with a different build: retained images must
remain tagged in the `garmin-ai` repository so retention can verify their identity.
Cleanup refuses to proceed if either retained image loses this identity.
Cleanup does not recurse into
subdirectories. Only regular files older than 24 hours with this name pattern and a
Docker `manifest.json` containing exclusively `garmin-ai:` image tags are eligible.
Fresh uploads, links, unrecognized archives, other repositories, and all files in
backup/data/token directories are preserved. Installation archives can be removed
even for retained releases: both complete images remain loaded in Docker for use
and rollback. A changed file or image tag stops deletion.
Before deletion, an eligible archive is moved atomically into a private temporary
directory and its identity is checked there. A replacement at the original upload
name is preserved. Changed files are restored without overwriting another upload;
if restoration conflicts or deletion fails, the file is kept in the reported
`.garmin-ai-cleanup-*/archive` path for operator recovery. Empty temporary
directories are removed after successful cleanup. These directories are never
recursively pruned.

Cleanup removes only unused tagged `garmin-ai` images. Images referenced by other
containers, including failed migration containers, are protected. Successful exited
`migrate` containers for obsolete releases are removed without their volumes so
they do not pin an old image indefinitely. There is no global Docker prune, forced
image removal, volume removal, backup retention change, or traversal of private
application directories. Small historical release configuration folders are kept.

During future deployments, hold the same host lock around all uploads, replacements,
image loading, and rollout verification. This also coordinates writers that might
already hold an open file descriptor. For example, open a root shell with:

```sh
sudo flock /run/lock/garmin-ai-release-cleanup.lock sh
# Keep this shell open while uploading from your deployment terminal.
# Complete uploads, load the image, and verify the rollout while the lock is held.
# Exit this shell to release the deployment lock before recording the release.
exit
sudo python3 /usr/local/lib/garmin-ai/release_cleanup.py --apply --record-only
```

Record each successful rollout immediately. Once both application containers are
healthy on the same new image, the previously retained current image becomes the
rollback. A daily cleanup also detects this transition if the hook was missed.
Mixed versions, stopped application containers, missing retained images, or a
running migration prevent cleanup. A new current image must pass both health
checks before retention can advance. Once the current and rollback identities
have been verified and recorded, a slow health probe on a busy host does not block
cleanup of unrelated old artifacts: both retained images stay protected. This
allows space recovery even when resource pressure delays health probes.
The shared lock makes simultaneous cleanup skip safely.
Preserve `/var/lib/garmin-ai-release-cleanup/state.json` across deployments;
it contains only image identities and upload directory paths, never credentials.

For a later read-only preview, run:

```sh
sudo python3 /usr/local/lib/garmin-ai/release_cleanup.py
```

After cleanup, verify application readiness, retained images, free space, and backup
space readiness. Do not remove the retained rollback image manually. To pause this
maintenance, disable the timer and wait for any active service to finish:

```sh
sudo systemctl disable --now garmin-ai-release-cleanup.timer
sudo systemctl stop garmin-ai-release-cleanup.service
```
