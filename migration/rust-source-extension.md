# Native source extension protocol version 1

Build the independently packaged, synthetic walking source with
`cargo build --locked --manifest-path examples/native-source/Cargo.toml`. Run
`garmin-ai-rust check-source-extension /absolute/path/to/garmin-ai-synthetic-source source:synthetic:probe`
to check its declaration, pagination, and observations. The check uses a fixed synthetic
calendar range and reports the page and record counts. It leaves extension installation and
worker activation to the operator.

Each exchange starts the configured executable once and sends one UTF-8 JSON line on standard
input. The executable writes one JSON line on standard output, closes it, and exits successfully.
The request contains `version: 1`, an integer `request_id`, `instance_id`, `method` and `params`.
The response echoes `version` and `request_id` and supplies exactly one of `result` and `error`.
The bounded error codes are `unsupported`, `invalid_request` and `unavailable`.

`describe` receives null parameters and returns the source kind, exact instance ID,
implementation version, and the existing source capabilities document. `read_page` receives a
half-open UTC window as `start` and `end`, a nullable opaque `cursor`, and an integer `limit`.
Its result is the existing source page document. The host validates the instance, time semantics,
declared operations, page limit, cursor chain and record window before accepting the output.

The host passes a cleared environment, suppresses child error output, and applies bounds to both
directions and to the duration. It kills and waits for the direct child after a deadline or
cancellation. The executable still runs with the host account's operating system permissions.
Run trusted adapters in a separately isolated environment when they need lower privileges.

The Linux ARM64 native check matched the pinned Python example's two pages and three records.
`check_source_extension.py` also exercised malformed envelopes, version and request mismatches,
undeclared capabilities, a repeated cursor, an out-of-window record, deadlines and cancellation.
The independent executable and native check ran together with networking disabled at 32 MiB.

## Reading a configured source

`garmin-ai-rust poll-source-extension /absolute/path/to/adapter source:sample:one` reads up to
ten pages for a currently enabled item in `GA_INTEGRATIONS`. The database's saved source
selection and retry time are checked before starting the adapter. The request's optional
`context` carries the instance's `config` and resolved `secrets` over standard input. Only
explicitly referenced `GA_PLUGIN_` variables enter this context. The existing mapping
confirmation uses the same configuration fingerprint as Python.

Each accepted page commits its raw observations, mapped values, and next cursor together.
The next invocation resumes that cursor after a failure. The paired PostgreSQL fixture
confirmed an interrupted second page, cancellation during the publication transaction, and
matching final records and mapped values in Python and Rust.
