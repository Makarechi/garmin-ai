# Initial differential cases (R03, part 1)

The [inputs](contracts/initial-cases.json) and
[reference observation](contracts/python-initial-observation.json) pin a small compatibility
corpus from the Python baseline. The capture exercises the Python CLI, FastAPI application, real
MCP stdio server, PostgreSQL side effects and content-addressed archive.

- Archive objects preserve distinct missing, zero and null values, exact UTF-8 bytes, large
  integers, float spelling and case-specific hashes. A nonfinite number does not create a file.
- The HTTP case creates a synthetic diary note at the first occurrence of a local clock time
  during the autumn DST transition. It covers missing credentials, read-only credentials,
  invalid input, identical retry, conflicting retry, stale revision and a read after reopening
  the app and pool. It records all event and audit rows. One event and one audit record survive.
- MCP starts as a fresh stdio process, lists read tools, reads the stored note, rejects invalid
  input and refuses a write with writes disabled. Structured results agree with text results.
- CLI cases capture exit status, stdout and stderr for inventory and validation failures.

## Reproduce

Use the locked full installation from the Git SHA in the observation. Prepare two independent,
empty PostgreSQL/TimescaleDB databases ending in `_test`, on an isolated test network. For each
database supply `GA_TEST_DATABASE_URL` and run the following from the root of the pinned checkout.

```sh
uv run --no-sync python /path/to/migration/capture_reference.py \
  --cases /path/to/migration/contracts/initial-cases.json > observation.json
```

The capture applies the pinned schema on the fresh database and leaves it populated for
inspection. Use a fresh database for another run. The benchmark uses a fresh private archive
directory for each run, discards inherited provider settings and never serializes bearer tokens
or the database URL.

Compare independently collected observations and run the comparator's mutation tests.

```sh
python migration/differential.py reference.json candidate.json
python -m unittest discover -s migration -p test_differential.py
```

The comparator ignores only the top-level implementation metadata. Arrays are ordered, JSON
types are distinct, and an absent key differs from `null` and `0`. The diary capture labels
generated UUIDs and the generated `created_at`, `updated_at`, `recorded_at` and `ingested_at` times
in order of first appearance, including the canonical envelope and audit. It checks that each
raw UUID is valid and each generated timestamp has an offset. Keeping the relationship between
observations exposes changed identity or timestamp reuse. Source timestamps, revisions, payloads,
permission errors, archive keys and content bytes retain their values. The Rust capture should
emit the same format and use the same explicit requirements for generated values.

Two separate Python captures matched the committed observation exactly. The mutation tests
reject missing-to-zero conversion, duplicate facts, wrong revisions and wrong hashes. R03 still
needs archive replay and interruption cases, broader state comparisons and the Rust capture
adapter when its runtime is available.
