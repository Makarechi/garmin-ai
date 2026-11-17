# Verified timing-only sleep read

The timing-only read verifies one wake-date interval against a single joined
snapshot of the raw response, its parser version, the current watermark and
the stored interval. Missing data and unverifiable data retain their distinct
statuses. The read checks the storage lifecycle and uses the bounded control
pool so unrelated replay does not fill its working connections. The result's
digest matches the Python format used when checking a previously prepared
answer.

The native Linux ARM64 fixture matched pinned Python for 26 synthetic nights
and every result hash. It ran while another transaction held the normal replay
mutation lock and separately confirmed that the storage lifecycle lock still
blocks the read. The fixture includes both sides of midnight, subsecond values,
an empty latest attempt, malformed provenance and clocks, and rejected parser
versions. No provider calls are involved.
