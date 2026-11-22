# Recover a native release transition

Select recovery using the recorded write freeze, the current schema/parser
versions and the newest acknowledged native operation. Stop application writes
and all pollers and senders before moving ownership. Preserve queued,
running, accepted and uncertain deliveries, their receipts and the current
polling cursor. Record any external ambiguities for reconciliation.

## Failure before native writes

Compare the last acknowledged operation, queue and archive inventories with the
pre-switch record. Verify the pre-switch encrypted backup with its retained key,
then restore into the prepared empty recovery database and private storage
directories. Restore raw files and tokens together with database state and
installation configuration, with the active runtime stopped.

Run the original digest-pinned release against the recovered copy while
external ingress and outbound deliveries stay paused. Confirm its schema
status, exact owner binding, API readiness and stored records. Check the
pending work and delivery states and confirm that only its worker owns the
database runtime lease before reopening the authorized interfaces.

## Failure after native writes

Freeze the native runtime and create a new verified backup while retaining
the original archive. Capture the difference of acknowledged mutations,
audit revisions, archive references, jobs and receipts between the two
recovery points. A pre-switch snapshot alone leaves the post-switch
operations outside the recovered state.

Prefer recovery with a compatible native image when the new state can be
read and verified as a whole. If a return to Python is required, first
validate the old reader against the recovered post-switch schema and data.
If compatibility fails, restore the older snapshot to a separate target and
reconcile each approved post-switch mutation from the captured change
record. Preserve idempotency keys and audit history. Check each resulting
record and queue receipt before serving the recovered installation. That
decision requires approval for the named target and reconciliation method.

## Restore and verify

Authenticate the archive before extracting it. The digest-pinned bundle's
`./install.sh unpack-backup ARCHIVE.enc` creates a private recovery
directory. On the isolated recovery target, confirm the database against the
installation settings and check its schema with `./install.sh migrate --status`.
Keep its API and worker stopped. Restore the authenticated raw and token files
to the exact configured destinations with their private ownership. Run
`./install.sh restore-db RECOVERY_DIRECTORY` with the directory name printed by
`unpack-backup`. It loads `database.jsonl.gz` and requires an empty database at
the supported revision. Verify the result before starting readers. Keep the
source installation and both encrypted backups until recovery has been verified.

Python and Rust coordinate unpacking through a private workspace in the
recovery destination's parent directory. When unpacking stops partway through,
retry using the same parent so the application can discard the interrupted
plaintext and authenticate the archive again. A running unpack owns the lock
until it exits. Native unpack checks for redirects and overlapping live storage
before cleanup, including alternate path spellings on case-insensitive volumes.
The `.garmin-ai-plaintext` name is reserved for staging. Earlier Rust releases
used separate `.rust-backup-*` folders.
Treat any of those retained folders as private and confirm the old unpack process
has exited before handling them.

The native cleanup checks for mount points and protected files before removing
an interrupted workspace. If it refuses the workspace, inspect the mounts and
choose a separate recovery parent where the archive and live storage are outside
the recovery workspace.

Check the recovered table counts and stable identity hashes, archive
manifest and file hashes, parser/representation versions, source account
binding, polling and replay offsets, and pending clarification. Compare
outbox state and receipts before re-enabling a sender. A lost provider
response stays uncertain until the authorized reconciliation distinguishes
accepted work from work that must be retried. Use the persisted ordering and
receipt states when processing the remaining queue.

Measure recovery time from detection to verified service readiness and
resumed work. Record the last acknowledged operation and the preserved or
reconciled result for each post-switch operation. The synthetic
[recovery checks](README.md#rehearse-recovery-before-and-after-native-writes)
measure archive authentication, database restore and file placement in the
fixture. Measure readiness and backlog recovery on the actual rehearsal host.
