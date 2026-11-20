# Rust migration – JSON import rows

The JSON reader uses the existing 512,000-byte and 500-row limits. It
rejects duplicate keys throughout the document and accepts decimals only
when Python's shortest finite float representation preserves their exact
decimal value. Integers keep their full supported precision. Escaped
unpaired surrogates and NUL characters yield row-level reasons with no
source text, in the same order as the Python preview.

File import checks for duplicate keys when each object closes. Malformed
syntax and invalid numbers encountered before that point take precedence,
as in the Python decoder. Once an object's duplicate is detected, the
reader validates its remaining values without retaining them. Nested
objects still check their own keys. The 500,023-byte synthetic duplicate
case with a dense array peaked at 7,835,648 bytes under a 32 MiB
Linux ARM64 limit, with zero swap and zero memory-limit or OOM events.
Preview and apply report file-reader failures with the same redacted CLI
error as the pinned Python release.

`check_file_json.py` compared 260 synthetic outcomes against pinned
Python 3.15.2 on Linux ARM64. The native test ran in a network-isolated
32 MiB container with a peak of 22,118,400 bytes, zero swap and no
out-of-memory events. Cases include a whole file at the byte ceiling,
500 object rows, a dense array, conflicting Unicode escapes, negative
zero, exact decimals and 4,300-digit integers.

The file reader now parses, serializes and disposes of nested objects and
arrays iteratively. It retains the 512,000-byte and 500-row limits. The
container budgets reflect the relative cost of nesting arrays and objects
in the pinned Python release on each Linux architecture. Values very
close to the reference runtime's stack limit can still differ by shape
and environment. The chat JSON parser has its own depth limit.

On the frozen Linux ARM64 runtime, a representative array chain accepted
43,496 levels with an 8 MiB process stack and 21,642 with a 4 MiB stack.
Object chains and alternating array/object chains showed the same dependence
on process configuration. Portability at this boundary remains recorded in
[the deviations register](deviations.md).

The extended fixture has 270 matching outcomes on macOS and Linux ARM64
with the pinned Python reference. It covers accepted 42,000-level array
chains, 28,000-level object chains and mixed container chains, including
matching canonical row digests, and rejected excessive or malformed
documents. The Linux fixture ran in a network-isolated 48 MiB container with
a 46,362,624-byte peak, zero swap and zero OOM events.

The expanded 276-outcome fixture matched on Linux ARM64 and emulated Linux
AMD64. On AMD64 the prior native reader rejected 44,000 nested arrays accepted
by Python. The updated reader matched the Python digests at 52,000 nested arrays,
30,600 nested objects and 19,300 alternating array and object pairs. It also
matched the rejected inputs for all three shapes. The emulated AMD64 test peaked
at 63,516,672 bytes with zero swap or out-of-memory events in a 128 MiB container.
A 48 MiB emulated run was killed before finishing the same suite.

Run `check_file_json.py` with `--properties` to include seeded values from
every finite IEEE 754 exponent field, two equivalent decimal spellings per
value, and nested Unicode keys and strings. The Linux ARM64 comparison
matched all 4,475 outcomes, including 4,099 canonical row digests and 53
file-level rejections. The test container peaked at 58,724,352 bytes under
its 96 MiB limit, with zero swap and zero memory-limit or OOM events.

The normal import command now matches the frozen preview and confirmation
of a 1,200-level JSON row. Rust applied the confirmed file, and Python
replayed it as an existing row. The stored payload contains the terminal
value at depth 1,200. The same fixture verifies CSV import and replay,
redacted preview errors, mismatch rejection and whole-file rollback.

The normal CLI preview, apply and retry of 500 synthetic rows used a
505,414-byte file. The Linux ARM64 run peaked at 27,684,864 bytes within a
32 MiB limit, with zero swap and zero OOM events.
