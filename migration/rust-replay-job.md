# Lease-bound offline Garmin health replay

The replay lane claims work for the installed parser independently of Garmin
request pacing. It verifies the current lease before reading the archive,
selects the calendar or intraday replay, and finishes the job using the
database clock in the transaction that publishes the projection.

A native Linux ARM64 test forced the lease to expire during a projection
write. The old projection and parser version survived and no replay result
committed. A newly claimed lease recovered the same job. The test also
checked that an obsolete parser target stayed pending, archive corruption
deferred retry without changing the projection, and a cancelled intraday
replay left the sample untouched. Retrying the intraday job installed the
verified sample and completed the job. Peak memory was 8,286,208 bytes in a
32 MiB container, with no swap or OOM events.
