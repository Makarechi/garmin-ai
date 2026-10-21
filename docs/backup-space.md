# GA-27: backup disk preflight

`garmin-ai backup-space /backup/snapshot.enc` reports numeric free/required bytes and
staging/destination/shared roles without file paths or database contents. It writes
no backup and needs no provider or Garmin calls. `backup` and scheduled creation
apply the same preflight before plaintext export, then recheck actual compressed
export size before streaming the archive.

The initial export allowance is eight times PostgreSQL database size, at least 1 MiB.
Raw/token file sizes plus tar overhead are included. The tar is gzip-compressed at
level 9 and streamed directly into encryption, so only the compressed database
export and encrypted output occupy disk during creation. The estimate assumes
incompressible archive contents, with an extra 1% plus 64 KiB for gzip overhead;
it does not depend on past compression ratios. Separate volumes receive independent
checks. Each volume retains a 256 MiB margin. Existing snapshots are never deleted
to make room for a new one. Retention runs only after a completed, authenticated
snapshot is published. Recovery automatically recognizes both older uncompressed
tar contents and new gzip-compressed tar contents inside the same encrypted format.

This is an estimate and not a disk reservation: unusual serialization expansion,
concurrent writers, thin provisioning and remote quota changes can still exhaust
space. Existing exception cleanup and exclusive publication remain in force. No
physical-media assurance is implied. An early periodic Telegram disk alert and
configurable host-level storage budgets are subsequent phases; this preflight does
not claim those are complete. Tests use synthetic files and disposable PostgreSQL.
