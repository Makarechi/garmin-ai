# Native offline archive maintenance

`garmin-ai-rust replay` runs the archive planner and replay queue against the configured database and opens the existing `raw` archive inside `GA_DATA_DIR` read-only. It uses `GA_DATABASE_URL` and `GA_TIMEZONE` from the process or `.env`. `replay --once` performs a single planning pass and processes at most one due job.

The command claims the same runtime lock as the Python worker. It uses the existing stored Garmin identity, records no new enrollment, and keeps working when Garmin requires reauthentication. Planning uses the existing repair and continuation records. The worker waits fifteen minutes when planning is idle and checks again after thirty seconds when there is more work or the planner was busy. The lease protects a running replay, including when the worker is interrupted. A replacement worker can recover it after expiration.

The default archive limits are 8 MiB for JSON, 100 MiB for original FIT or ZIP, 4,096 ZIP entries, and 120 seconds for FIT verification. Pending records with malformed parser targets and obsolete repair metadata no longer stop an otherwise valid planning pass.

The packaged command restored a synthetic daily projection from the archive, recovered an expired claim, refused to overlap the Python runtime lock, and released the lock after SIGTERM. It rejected access after the erasure marker was set. Linux ARM64 used 13,144,064 bytes in a 32 MiB container with no swap. The existing 100-job planner, repair and continuation fixture also matched Python.
