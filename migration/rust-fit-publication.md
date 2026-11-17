# Atomic activity FIT publication

The legacy ingestion adapter verifies the owner and training collection setting
before writing an original export. Original FIT and ZIP bytes retain their
legacy content-addressed key. An existing source row is reused when a payload
returns. The same archive bytes are verified again before stage preparation.

Publication rechecks the owner, consent, activity and request order under the
baseline mutation and activity locks. It replaces the existing FIT rows and
updates the archive pointer, parser version and attempt clock in one
transaction. Empty and outdated attempts leave the existing FIT rows alone.
A failed decode or database write retains the archived original and the last
successful pointer with the new failure identified separately.

The native Linux arm64 test ran against a disposable TimescaleDB. It checked
owner mismatch, a missing collection grant, an original matching its archive
digest, a late CRC failure after 514 decoded rows, out-of-order correction,
a forced SQL constraint failure, retry, duplicate detection and an empty
response. Non-FIT activity details survived each replacement.

The adapter supports direct publication from a captured receipt. The production
job lane still needs lease fencing and recovery wiring. Individual extracted
ZIP members and offline replay are additional compatibility steps.
