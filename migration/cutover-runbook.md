# Native release cutover

Use this procedure with the digest-pinned release bundle. Before a production
change, record the authorized service, environment, host and Compose project,
database instance, database and schema, artifact digest, operator and planned
window. Match the deployed revision to the schema tool bundled with that
revision. Record current database status and confirm the target over the same
connection route before any upgrade. Keep private connection details and
backup keys in the installation secret store.

Complete the release gates and run this procedure on an isolated recovery
installation first. Record the approved compatibility limits and the last
known working Python release, including its image, configuration and
supported schema/parser versions. Prepare the [rollback procedure](rollback-runbook.md)
and record who can authorize return to service.

## Rehearse with synthetic data

Use the frozen Python test environment and the exact native release binary.
Supply a new disposable TimescaleDB database through the fixture environment.
Names must end in `_test`. The restore check uses three distinct databases on
the same isolated server, through `GA_TEST_DATABASE_URL`,
`GA_TEST_BEFORE_RESTORE_URL` and `GA_TEST_AFTER_RESTORE_URL`. The worker check
uses a separate fresh database with `GA_TEST_DATABASE_URL`.

```sh
python migration/check_cutover_restore.py "$GARMIN_NATIVE_CLI"
python migration/check_worker_cutover.py "$GARMIN_FROZEN_PYTHON_CLI" \
  "$GARMIN_NATIVE_CLI" --replay-backlog
```

The experiments verify retained records before and after native edits,
encrypted archives, cursors, account bindings, clarification, pending jobs
and uncertain delivery. They also exercise exclusive worker ownership,
interrupted replay and the Python worker's return. Record the exact image
and the fixture restore timings. Use a separately provisioned recovery
installation to measure the operational recovery interval.

## Switch the authorized installation

1. Verify the old service is healthy and record the worker heartbeat,
   pending/running job counts, newest known polling cursor, archive inventory
   and backup freshness. Record checksums and aggregate counts in the private
   change record. Preserve unresolved deliveries and pending clarifications.
2. Pause scheduled initiators and remove write access at the installation
   boundary. Freeze imports, wearable ingestion, Telegram webhook or polling,
   web/HTTP writes, MCP writers, source adapters and backup jobs. Wait for
   active work to complete or for its recovery state to be durably recorded.
   Stop the old API and worker and disable their automatic restart. Keep the
   database available for backup and the schema check.
3. From the original bundle, run `./install.sh backup` with the API write
   boundary closed. The command stops the worker and verifies the encrypted
   archive. Record the completed archive's digest and timestamp. Copy and
   verify it on the prepared recovery host with the original backup key
   available separately. Capture the source configuration and volume mapping
   in the private change record.
4. Confirm the old worker and all old sending or polling loops have stopped.
   Use the native bundle's preserved instance settings and volume mapping.
   Verify the image reference in `release.env`, run `./install.sh setup` to check
   the preserved mounts and settings, and record `./install.sh migrate --status`
   against the authorized database. Apply the prepared upgrade with
   `./install.sh migrate` and confirm the current revision with `--status`.
5. Run `./install.sh inspect` for the loopback read-only API. Check liveness,
   readiness, owner and source binding, raw archive hashes, and representative
   diary and health reads. Compare cursors and frozen work against the
   pre-switch record. The inspection process holds the database runtime lease
   so neither worker can start during the review. Keep external access and
   other writer processes stopped. Exit inspection with Ctrl-C when finished.
6. Start the verified bundle with `./install.sh start`. Confirm worker
   heartbeat, source replay and exclusive ownership before reopening access.
   Check resource pressure and that the previous verified backup is still
   recoverable. Resume only enabled routes and consumers. Track
   the first acknowledged write and the first post-switch lease. Verify
   distinct successful, pending and uncertain deliveries and an ordinary
   archive write/read. Observe committed job counts, queue age, retransmission
   and backup health during the authorized window.

The bundled start command also checks and applies the bundled migration. The current
installation's [release procedure](../docs/release-install.md) defines its
directory layout and environment. If a startup, consistency or ownership
check fails, keep writes closed and follow the selected recovery path.
