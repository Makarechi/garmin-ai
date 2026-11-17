# FIT wire reader

R14 found that swapping in a decoder which uses the profile's type in place of
the wire definition discards encoded values. The native reader keeps byte order,
local and global message numbers, offsets, physical order and the entire field
data. A developer field's number and index are independent of its native-field
association. Sequence boundaries reset definitions and compressed timestamps.

The callback receives candidates. A read, cancellation, malformed definition,
or checksum error invalidates the result, including messages already visited.
Callers must keep candidates out of the published projection until the complete
member and its enclosing export have passed validation.

The synthetic cases produced with fitdecode 0.11.0 cover chained sequences,
compressed timestamp wraparound, field redefinition, a big-endian unknown
message and developer bytes with no descriptor. A native Linux arm64 release
test compared every field of 409,600 records including the short final record in
the 100 MiB synthetic R14 fixture. All five tests ran in 0.27 seconds under a
32 MiB cgroup, with swap disabled and no OOM events. The peak included file
cache and reached the limit with reclaim.

The legacy profile and its derived values will be applied above the wire reader.
This reader does not generate ActivityPart rows or publish a projection by itself.
