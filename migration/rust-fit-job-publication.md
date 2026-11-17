# Garmin FIT queue publication

The existing FIT queue grants a lease to one worker. The archive and projection
code checks that the lease names the same activity and Garmin account before
capture. It checks the lease again in the database transaction that updates the
current activity, freshness and job result. The database clock is used at
publication. A lease that expires during row replacement rolls back publication
and the previously verified activity remains available. The original archived
download can be reused.

A successfully downloaded corrupt export advances the fetch and success clocks
without advancing normalization. Provider fetch failures preserve both previous
success and normalization clocks. Older fetches cannot roll back freshness.
Provider cooldown refunds an attempt and schedules the supplied retry.

A synthetic ARM64 Linux TimescaleDB test forces expiry during the candidate row
insert, verifies that the old rows and freshness survive, and reclaims the job.
It also exercises bad CRC, cooldown, mismatched activity and stale responses.
No background acquisition or live Garmin access is enabled by this change.
