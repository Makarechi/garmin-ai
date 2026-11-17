# Rust migration – calendar health reads

Native reads return the stored daily summaries, the legacy metric contracts
and explicit missing metrics. They preserve date ordering, gaps, zero values,
provider provenance and the original ingestion timestamp. Daily summaries
remain calendar evidence.

The Linux ARM64 executable matched twelve outcomes from pinned Python over
a disposable TimescaleDB schema. Cases include a missing day, a one-day
range, the exact 3,660-day request bound, reversed and extreme dates,
timestamp precision, a signed zero and a retained pending raw response.
