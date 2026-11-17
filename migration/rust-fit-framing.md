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

## ZIP activity exports

The reader walks the seekable ZIP central directory in archive order using the
pinned `zip` 9.6.1 iterable API. It validates member headers and rejects unsafe
paths and symbolic links without ever extracting a filename to disk. It checks
the configured entry count, input bytes, aggregate declared FIT bytes, actual
expanded FIT bytes and processing deadline. It streams one FIT member at a time,
checks both layers of checksums and reports member and sequence positions. A
failure anywhere in the export invalidates the earlier candidate positions.

This step accepts Stored and Deflate members. A different compression method
returns an explicit unsupported result. Additional formats in Python's ZIP reader
need independently bounded decoder configuration before they can be enabled. A
nested ZIP is not read as a second archive.
[Python ZIP formats](https://docs.python.org/3.15/library/zipfile.html)
[ZIP iterable API](https://docs.rs/zip/9.6.1/zip/unstable/read/struct.ZipIterable.html)

Five native Linux arm64 tests passed in a 32 MiB container without swap. They
include unsafe paths and symbolic links, false size metadata, both checksum
layers, multiple members and sequences, a deadline and cancellation. The exact
100 MiB FIT member compressed to a 407,262-byte ZIP. All five tests ran in 0.27 s
with 7,589,888 bytes of peak cgroup memory and zero OOM events. This measures ZIP
admission, expansion and checksum validation. It does not measure the later
FIT field decoding or database work.
