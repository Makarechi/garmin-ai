# System diary payload validation

The Rust domain validates the eight system payload families from Python `EventInput` and all twelve context types. It preserves nullable medication values, distinguishes strict user-entered ratings from the older coercing fields, validates reported symptom and wellbeing observations, and leaves estimated caffeine values bounded by their reported range. Strings follow the Python limits in Unicode code points. The validator returns a normalized JSON value suitable for the existing payload column.

The differential check compared 1,707 synthetic inputs with the Python baseline at `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. Accepted values and rejection decisions matched on the development host and a native Linux arm64 release test. Cases include the full kind list, unknown fields, ranges, Unicode, repeated keys, UUID forms, optional fields and the distinction between `-0` and a floating-point zero in strict integer fields.

The event envelope, source and status rules, database relations, audit and undo are separate. Before writing a medication event, the event envelope must verify that a record with unknown values is a confirmed reported intake.

```sh
uv run python migration/check_system_payloads.py > test-results/system-payloads.ndjson
GA_TEST_SYSTEM_PAYLOADS="$PWD/test-results/system-payloads.ndjson" \
  cargo test --locked -p ga-domain --test event_payload -- --include-ignored
```

## Event input and provenance

The event input keeps distinct observed, recorded and ingested times. It validates
known timezones, the source and status, and the differences between point events
and reports covering a time interval. Missing medication fields remain unknown
only for a confirmed reported intake. The audited snapshot mode follows the
baseline rules used to restore earlier edits.

Date input uses the same speedate 0.17.0 parser as Python's pinned Pydantic Core
2.50.1. This preserves the handling of compact offsets, longer fractions, numeric
seconds and milliseconds, and Python's distinct behavior for negative numeric
values and numeric strings. Stored times are represented as UTC instants at
microsecond precision. Local times with different offsets remain distinct.

Provenance derives device, Telegram, MCP and API attribution from the authenticated
actor. Wearable clock uncertainty is available only to an authenticated wearable
actor and preserves the baseline unknown precision when the uncertainty is nonzero.
The validation comparison passed for 1,842 generated Python cases on the development
host and native Linux arm64. Separate Rust cases verify provenance and that an API
actor cannot claim device attribution through an input field.

```sh
uv run python migration/check_event_inputs.py > test-results/event-inputs.ndjson
GA_TEST_EVENT_INPUTS="$PWD/test-results/event-inputs.ndjson" \
  cargo test --locked -p ga-domain --test event_input -- --include-ignored
```
