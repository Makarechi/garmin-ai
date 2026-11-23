# GCP host release retention

The daily host timer retains the current `garmin-ai` image and one verified rollback
image. It runs at 12:00 UTC, with up to 15 minutes of jitter, at low CPU and I/O
priority. Missed runs are caught up after boot. It does not require the operator's
computer, a chat session, or the application scheduler to remain open.
If the host is still starting, a new deployment fails its health check, or another
safety check fails, the service retries after 30 minutes. It keeps both retained
images throughout partial progress and retries.

## Native release bundle

From the extracted bundle on the dedicated host, tag the digest-pinned
application image with a unique local `garmin-ai:` release tag. Keep a
distinct, previously verified `garmin-ai:` tag for rollback.
The timer tracks the `garmin-ai` Compose project.

```sh
image=$(sed -n 's/^GA_APP_IMAGE=//p' release.env)
docker tag "$image" garmin-ai:gcp-RELEASE_ID
sudo ./deploy/gcp/install-native-cleanup.sh --preview \
  garmin-ai:PREVIOUS_VERIFIED_TAG /opt/garmin-ai/uploads
sudo ./deploy/gcp/install-native-cleanup.sh \
  garmin-ai:PREVIOUS_VERIFIED_TAG /opt/garmin-ai/uploads
```

Create the upload directory before previewing. Review the selected images and
archives in the preview. Installation extracts the cleanup binary from the
digest in `release.env`, verifies or initializes the retained image record, and
enables the same daily timer. It uses the existing lock and state file, including
the image identities recorded by the earlier installer. For an existing state
file, pass the upload directory already recorded on the host.

When both application services use the same pinned image from a registry,
cleanup can also release that registry's digest references for obsolete,
locally tagged `garmin-ai` releases. It preserves images with other registry
references or repository tags. Use the same release repository for successive
deployments on this host.

Use the installed command for subsequent previews and rollout recording:

```sh
sudo /usr/local/lib/garmin-ai/garmin-ai-release-cleanup
sudo /usr/local/lib/garmin-ai/garmin-ai-release-cleanup --apply --record-only
```

## Retention and deployment lock

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
sudo /usr/local/lib/garmin-ai/garmin-ai-release-cleanup --apply --record-only
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
sudo /usr/local/lib/garmin-ai/garmin-ai-release-cleanup
```

After cleanup, verify application readiness, retained images, free space, and backup
space readiness. Do not remove the retained rollback image manually. To pause this
maintenance, disable the timer and wait for any active service to finish:

```sh
sudo systemctl disable --now garmin-ai-release-cleanup.timer
sudo systemctl stop garmin-ai-release-cleanup.service
```
