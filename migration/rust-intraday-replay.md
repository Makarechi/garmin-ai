# Rust migration – intraday sample replay

Intraday replay uses the verified application journal to recover overlapping
observations. It stages the affected identities in PostgreSQL and reads one
verified archive revision at a time. Retained samples keep their original
application and local date. Later authoritative intervals and unrelated
owners prevent deleted or displaced samples from returning. The final
projection, revision history, parser version and replay outcome commit together.

On Linux ARM64, five scenarios matched the pinned Python application on
separate TimescaleDB fixtures. They cover restoration of a previous valid
sample, a later authoritative deletion, an unknown legacy order, a damaged
preceding archive and repeated content after a correction. Verification
compared current measurements, revision history, application and ownership
journals, raw outcomes and shared daily fields. A forced SQL failure after
the reconstruction started preserved the previous value, and retry succeeded.
