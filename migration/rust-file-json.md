# Rust migration – JSON import rows

The JSON reader uses the existing 512,000-byte and 500-row limits. It
rejects duplicate keys throughout the document and accepts decimals only
when Python's shortest finite float representation preserves their exact
decimal value. Integers keep their full supported precision. Escaped
unpaired surrogates and NUL characters yield row-level reasons with no
source text, in the same order as the Python preview.

`check_file_json.py` compared 260 synthetic outcomes against pinned
Python 3.15.2 on Linux ARM64. The native test ran in a network-isolated
32 MiB container with a peak of 22,118,400 bytes, zero swap and no
out-of-memory events. Cases include a whole file at the byte ceiling,
500 object rows, a dense array, conflicting Unicode escapes, negative
zero, exact decimals and 4,300-digit integers.

The file reader now parses, serializes and disposes of nested objects and
arrays iteratively. It retains the 512,000-byte and 500-row limits. Its
container budget is below the measured Linux ARM64 reference limits for
the tested array, object and mixed shapes. Values very close to the
reference runtime's stack limit can still differ by shape and platform.
The chat JSON parser keeps its existing separate depth limit.

The extended fixture has 270 matching outcomes on macOS and Linux ARM64
with the pinned Python reference. It covers accepted 42,000-level array
chains, 28,000-level object chains and mixed container chains, including
matching canonical row digests, and rejected excessive or malformed
documents. The Linux fixture ran in a network-isolated 48 MiB container with
a 46,362,624-byte peak, zero swap and zero OOM events.

The normal import command now matches the frozen preview and confirmation
of a 1,200-level JSON row. Rust applied the confirmed file, and Python
replayed it as an existing row. The stored payload contains the terminal
value at depth 1,200. The same fixture verifies CSV import and replay,
redacted preview errors, mismatch rejection and whole-file rollback.

The normal CLI preview, apply and retry of 500 synthetic rows used a
505,414-byte file. The Linux ARM64 run peaked at 27,684,864 bytes within a
32 MiB limit, with zero swap and zero OOM events.
