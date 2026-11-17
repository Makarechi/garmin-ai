# System diary payload validation

The Rust domain validates the eight system payload families from Python `EventInput` and all twelve context types. It preserves nullable medication values, distinguishes strict user-entered ratings from the older coercing fields, validates reported symptom and wellbeing observations, and leaves estimated caffeine values bounded by their reported range. Strings follow the Python limits in Unicode code points. The validator returns a normalized JSON value suitable for the existing payload column.

The differential check compared 1,707 synthetic inputs with the Python baseline at `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. Accepted values and rejection decisions matched on the development host and a native Linux arm64 release test. Cases include the full kind list, unknown fields, ranges, Unicode, repeated keys, UUID forms, optional fields and the distinction between `-0` and a floating-point zero in strict integer fields.

The event envelope, source and status rules, database relations, audit and undo are separate. Before writing a medication event, the event envelope must verify that a record with unknown values is a confirmed reported intake.

```sh
uv run python migration/check_system_payloads.py > test-results/system-payloads.ndjson
GA_TEST_SYSTEM_PAYLOADS="$PWD/test-results/system-payloads.ndjson" \
  cargo test --locked -p ga-domain --test event_payload -- --include-ignored
```
