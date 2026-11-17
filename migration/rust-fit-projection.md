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

The conformance check compared 50,039 synthetic Python rows across 12 FIT files.
It includes FIT developer metadata, unknown messages, byte arrays, invalid
sentinels, compressed timestamps, component expansion, scaling and two chained
sequences. Native Linux arm64 decoded the 100 MiB source as 409,600 rows inside a
32 MiB limit without swapping or OOM. The test inspects each row as it is decoded.
The measured peak includes file cache. Database and archive writing are tested
separately.

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
