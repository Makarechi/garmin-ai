# Domain identity, time and text offsets

The source Garmin profile fingerprint uses the original decimal identity and SHA-256
namespace. The Rust code follows `accounts.profile_fingerprint` by accepting a positive
signed-64-bit integer or an ASCII decimal string of at most 19 characters. The tests
compare the known Python hashes, including zero-padded input and the upper endpoint.
The identity type omits debug formatting so routine diagnostic formatting cannot print the
raw Garmin identifier.

Stored instants use UTC and microsecond precision. Their canonical output matches
`datetime.astimezone(UTC).isoformat()`, including six fractional digits for a nonzero
fraction and `+00:00` in place of `Z`. Tests cover the two occurrences of a time at a DST
fold, a negative epoch fraction, a leap day and Python's supported year endpoints.
The offset parser here expects RFC 3339 for already-normalized timestamps. Input formats
specific to upstream adapters stay with those adapters.
[CPython datetime formatting](https://docs.python.org/3.15/library/datetime.html#datetime.datetime.isoformat)
[time OffsetDateTime](https://docs.rs/time/0.3.55/time/struct.OffsetDateTime.html)

The text offset conversion retains Python's Unicode code point indices and yields
UTF-8 byte ranges and Telegram's UTF-16 code unit ranges. The fixture combines an
astral character with a combining mark and checks both conventions against Python.
