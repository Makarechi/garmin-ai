# Extension contract and local test kit

The current Python extension contract is **version 1**. It uses the
`garmin_ai.integrations` entry-point group; entry-point names are
`source.provider`, `channel.provider` or `model.provider`. The owner must list
each enabled instance in `GA_INTEGRATIONS`. Installing a package alone never
enables it. A selected instance supplies validated non-secret config and named
`GA_PLUGIN_*` secret references to its factory through `PluginContext`.

Model adapters may use `JsonSchemaCompiler` to compile the supplied application
model; Gemini's compiler projects it onto the supported provider schema. The
original application model is always validated on return. Existing adapters can
return the validated value directly; an adapter can instead return `ModelResult`
with `ModelUsage` input/output token counts. The boundary validates those counts
and logs them with operation, outcome and latency, without prompt or response data.
Adapters should report refusals, truncated outputs and known failures using the
provider error types. The gateway bounds how long its caller waits; an adapter
may declare `cancellation` and implement `cancel()` for a best-effort stop.
Synchronous plugin code and remote processing are not forcibly terminated, and
the request gate remains held until that worker finishes.

An in-process Python extension is **trusted code** with the application's
process privileges. `PluginContext` narrows the intended interface but is not
a sandbox. Do not install an untrusted package or assume it cannot access the
filesystem, network or process environment. A separate process/container with
restricted capabilities would be needed for stronger isolation.

## Readiness and version policy

`CapabilityStatus` reports `contract_version`, `implementation_version`, named
capabilities and `verification_level`. `declared` means no runtime check was
performed, `local_configuration` means local dependencies and configuration
passed, and `unavailable` includes disabled or invalid instances. None of these
states is a live-provider test. Consent, owner selection, data availability,
rate limits and delivery are checked separately. Do not present a locally
available adapter as connected or live-verified.

Version 1 is experimental until a supported release. The application rejects
entry points with a different contract version. A breaking Python method or
data-shape change will increment the contract version; contributors should
declare the version they target and run this kit against every supported app
version. We will document a migration path and deprecation window **before**
declaring a stable public SDK; no compatibility window is promised for this
experimental contract. `implementation_version` describes the extension's own
package revision and is not a substitute for contract compatibility.

## Local examples and contract tests

From a checkout, without Garmin, Telegram or model credentials:

```sh
uv sync --locked
uv pip install --python .venv/bin/python --no-deps -e examples/synthetic-model
uv pip install --python .venv/bin/python --no-deps -e examples/synthetic-adapters
.venv/bin/pytest -q tests/test_synthetic_plugin.py tests/test_extension_tck.py
```

`examples/synthetic-model` implements a structured-output model and exercises
the actual agent and HTTP tracker path under explicit consent. The new
`examples/synthetic-adapters` package implements a bounded, cursor-based source
and a text-only in-memory channel that accepts fictional initiatives. The reusable checks in
`garmin_ai.extension_tck` test source page identity/window/cursor behavior,
the required source close lifecycle and at least one fictional observation,
channel acceptance evidence, declared rendering capabilities and model schema
output. The channel probe verifies only a basic text send and its receipt;
it requires a new text message containing that text and rejects unrequested
actions, attachments or reply references.
Advertised actions, attachments, voice, edit, reply and initiative capabilities
are listed as unverified in its result. Passing the probe does not certify
those optional paths. CI installs both packages independently of the
application source tree and runs these checks. Use only
fictional records and a disposable `_test` database for the model HTTP checks.
The kit revalidates returned capabilities, pages, delivery policies and attempts
before accepting their declared shape. A source must return a `SourcePage`
instance; a plain mapping does not pass the kit or the worker.
A channel must return a `DeliveryAttempt` instance; a plain mapping also fails
both paths. Its receipt must match the attempt or provide stronger accepted,
delivered or read evidence.
For a channel that permits only a known recipient, pass fictional `owner_id`
and `conversation_id` values to the channel probe.

The source page types in `garmin_ai.source_contracts` carry source record
identity, observed/effective times, timezone, original payload reference,
typed interval end times when interval semantics are declared,
partial versus complete-snapshot semantics, explicit deletions and a retry
deadline. Each record payload must be finite JSON, at most 16 KiB after
serialization, with nesting depth at most eight and at most 256 items in any
collection. The application must own persistence, deduplication, correction and
cursor commits. Stable record identities may repeat across page boundaries;
the probe counts each identity once while requiring cursor progress. A page
finishing pagination is **not** by itself a complete
snapshot or permission to delete missing records. The channel example uses the
existing `ChannelPort` and reports `provider_accepted`; it never reports
`delivered` or `read` without separate evidence.

The model plugin is part of the selected runtime. An explicitly enabled source
plugin is polled by the ordinary worker; saved onboarding preferences restrict
this to selected instances, while legacy installs without saved preferences use
the explicit configuration. Bounded pages, raw records, provenance, record
identities and cursor are saved in the
private database; the cursor advances only with its page. This generic path
polls a rolling seven-day window and processes at most ten pages per job;
each raw record is limited to 64 KB;
remaining pages continue on a later worker pass. An adapter can return a
`retry_after` deadline, and records for two configured instances remain
separate even when their source record IDs match. A completed page does not
delete missing records unless the adapter sends explicit deletion records.
Cursor fingerprints remain with the active window across jobs so a source
cannot cycle back to an earlier page; more than 1,000 cursors in one window
requires an adapter-specific pagination plan.
The current worker stores declared corrections and deletions as private raw
history, with an ordered revision marker when a record changes or returns to
an earlier value.
It does not normalize plugin observations into diary metrics or invalidate
analyses, so those records do not appear in analysis. The independent file
import handles its own explicit mapping and normalization. Garmin still has a
separate ingestion path. An explicitly enabled channel plugin advertising
`initiatives` starts and closes with the worker and may deliver neutral
reminders to its own channel instance. Saved onboarding preferences are checked
when claiming each initiative, so selecting a configured plugin later does not
require a worker restart. The worker checks intent identity and observed
delivery evidence before recording a result. An extension still needs an
authorized conversation and owner consent; this
does not provide a generic inbound transport or account-pairing route.
Telegram's direct ingress and reply path remains specific to Telegram. The
source and channel examples remain contract fixtures, not production provider
claims.
No external developer reproduction or published-package compatibility is
claimed until an independent contributor runs the guide against a published
release.

## Native channel probe

The native version 1 channel protocol uses one JSON request and one JSON reply
per process. It shares the source protocol envelope. The `describe` method
returns `kind: "channel"`, the configured instance ID, the implementation
version and the channel capabilities. `delivery_policy` and `deliver` take a
normalized outbound intent and the current timestamp in `params`. They return
the corresponding delivery policy and attempt documents described above.

Build and run the independent fictional channel from the repository root.

```sh
cargo build --locked --manifest-path examples/native-channel/Cargo.toml
cargo run --locked -p ga-app --bin garmin-ai-rust -- check-channel-extension \
  "$PWD/examples/native-channel/target/debug/garmin-ai-synthetic-channel" \
  channel:synthetic:probe
```

The probe checks a plain-text render, the recipient namespace, the delivery
policy and a matching receipt. It reports optional capabilities as unverified.
The example records `provider_accepted` for the fictional probe. The probe
is a standalone command for an explicitly supplied executable. The runner
clears the child environment and bounds each response and call duration. The executable still
runs with the application's operating-system permissions.

## Native model probe

The version 1 model `describe` reply identifies the `model` kind, instance and
implementation version. The `structured_output` capability must be true. The
`structured` request supplies the instruction, fictional prompt and original
application schema. The reply provides a `value` and can include optional
`usage` with integer `input_tokens` and `output_tokens`. The host validates
the returned value against its own schema and checks the usage counts before
reporting the result.

Build and probe the independent fictional model from the repository root.

```sh
cargo build --locked --manifest-path examples/native-model/Cargo.toml
cargo run --locked -p ga-app --bin garmin-ai-rust -- check-model-extension \
  "$PWD/examples/native-model/target/debug/garmin-ai-synthetic-model" \
  model:synthetic:probe
```

The probe checks structured output and accepts either Boolean value required
by the synthetic schema. Worker registration and permission checks are
separate from this local probe.

Declarative scenario packs have a separate data-only format documented in
[scenario packs](scenario-packs.md). The
[`focus-walks.json`](../examples/declarative-pack/focus-walks.json) example can
be opened in the dashboard, adapted and imported through owner preview and
confirmation. It is not Python entry-point loading.
