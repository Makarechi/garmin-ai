# Native Garmin periodic schedule

One scheduler pass plans frequent reads, wake-based sleep and HRV reads, evening snapshots, device inventory, and nightly reconciliation. It uses the owner's saved timezone after onboarding and preserves the seven-day nightly and thirty-day Monday windows. A paused connection leaves the schedule alone. When the source is deselected, pending Garmin jobs and their history windows are retired together. A source job already being handled retains its lease.

The schedule advances history windows and activity cursors in the same transaction as new periodic jobs. It keeps the existing deduplication keys and retires redundant pending fetches within each periodic family. The caller supplies the verified Garmin identity and configured source. This planner only writes the queue.

Eight synthetic passes matched pinned Python, covering up to 833 periodic records plus activity scans and history windows. The comparison included the DST overlap, restart, Monday expansion, a persisted timezone and wake time, connection pause, source deselection, and pack restrictions. A forced write failure rolled all scheduling changes back. The native Linux ARM64 run used 15,515,648 bytes under a 32 MiB cap with no swap. Eleven history passes, six activity-scan passes, and source-page policy regressions also passed.
