# FIT decoder selection

Status — use the native wire reader with the pinned 21.171 compatibility profile for
R15. Keep the pinned rustyfit and fitparser implementations as independent comparators.

The raw-field and checksum differences below motivated separating wire decoding from
profile interpretation. The native adapter can keep both the wire definition and every
raw value, and apply the same profile version as the Python application. Its projection
also matches the order and representation of derived and developer fields.

## Comparison

The synthetic corpus contains 16 cases. It includes two complete chained sequences,
dynamic field descriptions, an unknown record field, a big-endian manufacturer message,
compressed timestamps, an invalid value sentinel, optional and invalid header CRCs,
truncation, a late CRC failure, and a 100 MiB raw member. The probe hashes each decoded
message number and its sorted raw field values including developer index. It counts
headers, definitions, developer fields and CRCs independently. The trace compares decoded
values and boundaries. The ingestion adapter also needs to reproduce field order, names,
units, transformed values and message indices for existing projections.
[Measured corpus](../evidence/fit-decoder-linux-arm64.json.gz)

| Decoder | Pinned version | Profile |
| --- | --- | --- |
| Python fitdecode | 0.11.0 | Garmin SDK 21.171 |
| Rust fitparser | 0.12.0 | Garmin SDK 21.218 |
| Rust rustyfit | 0.11.1 | Garmin SDK 21.221 |

The Rust probe built from its lockfile offline for Linux arm64 and amd64. It ran the
developer fixture in the amd64 container under emulation and returned the same
decoder-specific hashes as the arm64 run. The process observations below were measured
on native Linux arm64 with the common raw-field trace. RSS includes the process and
benchmark startup overhead.

| Input | Python fitdecode | fitparser low-level processor | rustyfit stream |
| --- | --- | --- | --- |
| 50,003 messages, 500,128 bytes | 1.175 s / 66.56 MiB | 0.021 s / 17.86 MiB | 0.026 s / 17.86 MiB |
| 409,600 messages, 100 MiB | 51.49 s / 66.54 MiB | 2.36 s / 102.10 MiB | 0.424 s / 17.86 MiB |

The low-level fitparser probe reads the file into a slice and then releases each decoded
record. The rustyfit probe reads from a buffered file and releases each message before
advancing. Rustyfit also completed the 100 MiB fixture inside a 64 MiB container with swap
disabled and zero OOM events. The cgroup reached its memory ceiling with file-cache
reclaim. These timings include the observed field losses and describe the decoder
experiment rather than a complete FIT projection.

Both candidates preserved the synthetic 0xFF01 big-endian message and the unknown
field 222 on a known record. The legacy resource fixture's field 99 is respiration
in profile 21.171, and fitdecode also exposes a derived field 108. The measurements
behind R02 remain reproducible.

## Work required for native ingestion

- Preserve raw values from the wire definition. On the 100 MiB fixture, field 99 is
  encoded as bytes. Fitdecode returned the array. Rustyfit exposed its first value as
  the scalar `0`, using the newer profile's scalar type. The low-level fitparser trace
  retained the array but missed the derived field.
- Reproduce the Python field presentation. The developer descriptions agree on numeric
  IDs, and both candidates exposed developer value `202` on the last record. Rustyfit
  returned single strings as arrays for the description name and units. Its default
  component expansion returned raw field 108 as `200` where the Python trace had `2`.
  Normalize the invalid heart-rate sentinel to null while retaining the field. Preserve
  base-type metadata, timestamp conversion and profile-dependent names and units.
- Handle CRC framing explicitly. Fitdecode and fitparser accept the fixture that uses
  a zero header checksum with the header included in the final CRC. Rustyfit currently
  accepts the data-only variant and rejects the Python-compatible variant. Correct this
  before ingestion. Require a final CRC for each sequence. The low-level fitparser
  loop otherwise reaches EOF after reading the missing-CRC fixture. All three paths
  flagged the trailing-byte corruption or truncation before successful completion.
- Preserve complete sequence boundaries and delay durable projection publication until
  the final checksum of every included sequence passes. Rustyfit reconstructed the
  legacy compressed timestamp in this corpus. The low-level fitparser sample omitted
  the reconstructed timestamp. Record validation for headers outside the baseline
  12-byte and 14-byte forms. The synthetic 16-byte fixture hit a Python unpacking error
  and both Rust candidates returned a decoder error.
- Compare the translated output against the exact Python projection before enabling it.
  Keep each scope's input bytes and raw observation available to replay. Prefer a
  small reviewed upstream change or pinned patch for the missing wire fidelity and
  CRC handling. Keep fitparser as an independent diagnostic comparator.

[Garmin FIT protocol](https://developer.garmin.com/fit/protocol/)
[fitparser 0.12.0 raw API](https://docs.rs/fitparser/0.12.0/fitparser/de/struct.FitStreamProcessor.html)
[rustyfit 0.11.1 stream API](https://docs.rs/rustyfit/0.11.1/rustyfit/)

## Reproduce

From the repository root, use the locked project Python environment to generate
synthetic fixtures and the probe's independent Cargo lock to compile it.

```sh
cargo build --locked --release --manifest-path migration/fit_decoder_probe/Cargo.toml
PYTHONPATH=migration uv run python migration/fit_decoder_probe/corpus.py create test-results/fit-probe --ceiling
PYTHONPATH=migration uv run python migration/fit_decoder_probe/compare.py migration/fit_decoder_probe/target/release/fit-decoder-probe test-results/fit-probe
```
