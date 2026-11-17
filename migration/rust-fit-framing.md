# Bounded FIT framing validation

This validator reads a FIT stream through a fixed 32 KiB buffer. It checks the
header, the declared data length and the required trailing checksum for each
concatenated FIT sequence. It accepts the 12-byte and 14-byte header forms from
the Python baseline. A zero optional header checksum leaves the complete header
in the final checksum calculation, as in fitdecode 0.11.0.

It reports each complete sequence's position and header versions. A failure later
in the same member invalidates all earlier sequence positions for publication.
The validator rejects truncation and data after the last valid frame, and checks
cancellation before reading additional data. Empty input is reported as zero
frames so the ingestion layer can preserve the baseline's empty outcome.
[Garmin FIT protocol](https://developer.garmin.com/fit/protocol/)
[Decoder comparison](decisions/0003-streaming-fit-decoder.md)

Four fixed tests use synthetic single-message FIT sequences made with the pinned
Python encoder and verified by the Python reader. The same tests and the 100 MiB
ceiling fixture passed with the native Linux arm64 release test executable. The
container was limited to 32 MiB and had no swap. Peak cgroup memory reached that
limit with file cache reclaim and zero OOM events. This measurement covers
framing and checksums only. The ZIP, decoded values, database staging and atomic
projection publication remain part of the following ingestion steps.
