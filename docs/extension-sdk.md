# Extension contract and local test kit

The current Python extension contract is **version 1**. It uses the
`garmin_ai.integrations` entry-point group; entry-point names are
`source.provider`, `channel.provider` or `model.provider`. The owner must list
each enabled instance in `GA_INTEGRATIONS`. Installing a package alone never
enables it. A selected instance supplies validated non-secret config and named
`GA_PLUGIN_*` secret references to its factory through `PluginContext`.

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
before accepting their declared shape.
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
The current worker stores declared corrections and deletions as raw history.
It does not normalize plugin observations into diary metrics or invalidate
analyses, so those records do not appear in analysis. The independent file
import handles its own explicit mapping and normalization. Garmin still has a
separate ingestion path. An explicitly enabled channel plugin advertising
`initiatives` starts and closes with the worker when saved onboarding
preferences allow it, and may deliver neutral reminders to its own channel
instance. The worker checks
intent identity and observed delivery evidence before recording a result.
An extension still needs an authorized conversation and owner consent; this
does not provide a generic inbound transport or account-pairing route.
Telegram's direct ingress and reply path remains specific to Telegram. The
source and channel examples remain contract fixtures, not production provider
claims.
No external developer reproduction or published-package compatibility is
claimed until an independent contributor runs the guide against a published
release.

Declarative scenario packs have a separate data-only format documented in
[scenario packs](scenario-packs.md). The
[`focus-walks.json`](../examples/declarative-pack/focus-walks.json) example can
be opened in the dashboard, adapted and imported through owner preview and
confirmation. It is not Python entry-point loading.
