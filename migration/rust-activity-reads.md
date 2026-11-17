# Native activity reads

Activity summaries use the established half-open time range and optional
kind filter. Detail pages preserve the activity, ordered part rows,
truncation flag and next offset. The default includes reviewed FIT metadata
while raw samples and unrecognized FIT families require explicit opt-in.
The database determines whether another part exists without sending that
part's payload. The summary and page share one verified read snapshot.

Twenty-five ARM64 Linux requests matched pinned Python on disposable
TimescaleDB. The fixture checks every default FIT allowlist entry, unknown
families and raw samples, boundary clocks, invalid ranges and page sizes,
missing activities, Unicode, optional fields, and negative zero. A page
whose next payload was 16 MiB completed in 0.04 seconds and peaked at
7,806,976 bytes in a 32 MiB container with no swap.

These queries are ready for the separate activity tool integration.
