# Garmin archive capture under a live job lease

The capture path rechecks the saved job scope, owner and collection consent
before archiving the response. The job can renew while the archive is written.
The database records the source only while the same lease is current. A late
lease loss rolls back the source row. The immutable archive object can be
reused by a retry without creating another source identity.

The Linux ARM64 endpoint and existing raw-capture fixtures passed on
disposable TimescaleDB. A trigger expired the lease during source insertion
after the archive write. No source row committed, and immediate retries
created one row with the same digest. The endpoint fixture also passed the
separate publication rollback test. Peak memory across the two tests was
8,105,984 bytes in 32 MiB containers with no swap.
