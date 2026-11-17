# FIT projection compatibility

Native parsing uses the 21.171 profile from the pinned MIT-licensed fitdecode
0.11.0 reference. The profile is a development-time export embedded in the
binary. It covers 120 message types, 1,386 fields and their reference-based
subfields, enums and components. The original license is next to the profile and
must travel with distributions that embed it.

The projection preserves the current ActivityPart JSON shape, including ordered
fields, raw and presented values, offsets between compressed timestamps, local
clock values, enum names, optional developer descriptors and field association.
Definitions retain their existing descriptors across later descriptor changes.
The wire API also retains the exact bytes and base types. Source-dependent
timestamps remain tied to FIT data.

The conformance check compared 50,047 synthetic Python rows across 15 FIT files.
It includes FIT developer metadata, unknown messages, byte arrays, invalid
sentinels, compressed timestamps, component expansion, scaling and two chained
sequences. Native Linux arm64 decoded the 100 MiB source as 409,600 rows inside a
32 MiB limit without swapping or OOM. The test inspects each row as it is decoded.
The measured peak includes file cache. Database and archive writing are tested
separately.

Additional cases record importer behavior on ambiguous fields. A repeated
message definition resets the legacy component accumulator. The first field
with a duplicate name supplies the top-level value, and the ordered field list
keeps both the expanded and explicit values. Profile enumeration leaves an
array's numeric elements unchanged. Two checksum-valid heart-rate files
reproduce the pinned Python failures for a missing starting timestamp and an
unavailable compressed component. Their failure classes are distinguished in
the native projection, and the original wire values remain available.

Each row remains a candidate until the FIT checksum and the enclosing export
both pass. The next step owns validation of the complete activity export and
atomic publication to the existing database view.

From the project root, the reference corpus can be regenerated with the locked
Python environment. The generated fixtures contain synthetic values only.

```sh
PYTHONPATH=migration uv run python migration/fit_decoder_probe/corpus.py create test-results/fit-probe
PYTHONPATH=migration uv run python migration/check_fit_projection.py test-results/fit-probe test-results/fit-projection
GA_TEST_FIT_PROJECTION="$PWD/test-results/fit-projection" \
  cargo test --locked -p ga-fit --test projection -- projection_matches_the_pinned_python_records --include-ignored
uv run python migration/generate_fit_profile.py | cmp - crates/ga-fit/profile/21.171.json
```

## ZIP projection

The export reader checks each selected member in central-directory order. FIT
message indexes reset for each member and span the chained sequences in that
member. The exported row sequence spans all selected members. Validation of
both ZIP and FIT checksums continues through the final byte.

A native Linux arm64 test projected all 409,600 rows from the synthetic ZIP
with exactly 100 MiB of FIT data. All eight export tests finished in 3.40
seconds under a 32 MiB limit, with a 10,899,456-byte peak, no swapping and no
OOM events. A later corrupt member invalidated earlier callback candidates.
