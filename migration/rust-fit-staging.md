# Private FIT projection staging

The decoder feeds the existing ActivityPart row shape into the database one
bounded batch at a time. Each batch holds at most 500 rows and 256 KiB. A row
larger than the byte budget is sent in multiple chunks. The queue holds one
produced batch while the connection sends the preceding batch.

Candidates live in a temporary table scoped to the database connection and its
transaction. Other connections cannot see those rows. The database COPY is
completed only after every required ZIP and FIT checksum passes. The row count
returned by the database must agree with the decoder. A checksum failure
discards every candidate, including any rows sent before the damaged byte.

Both scenarios were tested with a native Linux arm64 client on disposable
PostgreSQL 17.11 with TimescaleDB 2.29.2. The first test staged 515 rows,
verified that a second connection could not read the candidates, rejected a
late checksum failure, and retried successfully. The second staged all
409,600 projected rows from the synthetic ZIP whose FIT member was 100 MiB,
then rolled back. On the current main-based branch, the tests took 39.08 seconds. The
client cgroup had a 32 MiB limit, a peak of 12,980,224 bytes, and no swap or
OOM events.
The database ran in a separate container.

The final publication step will take the mutation and activity locks and check
the current owner, source application order and job lease before replacing
visible rows. An admitted candidate is not yet a published import.
