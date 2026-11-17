# Durable model cooldown (GA-32)

The runtime also attaches the durable gate to explicitly selected external model
plugins when a database engine is available. Each plugin instance has its own
state and request lock; a rate limit on one instance does not pause another.
Changing non-secret plugin settings does not clear an active cooldown for the
same instance and credentials. Credential rotations have separate state, so
temporarily changing a credential does not erase the earlier credential's pause.
Onboarding consent is rechecked before each request. The existing Gemini state
key and configuration remain compatible across this change. Standalone plugin
construction without a runtime database still has no durable cooldown.

The plugin boundary validates structured results against the application's schema,
classifies known adapter and HTTP failures without exposing their payloads, and
records operation, outcome, latency and optional token usage without request
content. Refusal and truncated responses have distinct error types. Adapters
can use the common JSON Schema compiler; Gemini uses its own compiler. Domain
validation always uses the original application schema.

The runtime log preserves bounded model request metadata. For Gemini failures,
it distinguishes connection errors, timeouts, upstream service errors and rate
limits, and records an upstream HTTP status when available. These fields contain
no provider response body, credentials, message text or health data. A service
error or network failure still follows the existing cooldown policy; logging
does not introduce another request or a provider fallback.

Plugin calls have a bounded caller wait: `GA_MODEL_REQUEST_TIMEOUT_SECONDS`
defaults to 60 seconds, and a model instance can override it with
`request_timeout_seconds` between 0.05 and 300 seconds. A configured Gemini
instance also applies its override to the full approved model fallback chain;
without an override, its existing deadline is retained. When a plugin call exceeds the
deadline, the result is not delivered to the application. If the plugin declares
the `cancellation` capability, the gateway requests cancellation. A synchronous
plugin or its remote service may still be running; the per-instance/credential
gate stays locked until the worker exits so another call cannot overlap it.
Closing and cancellation are best-effort and cannot turn an abandoned response
into a diary write. Process termination cannot prove what the remote service did.

This phase requires one provider failure to pause later requests across text, voice,
analysis and background work; the pause must survive worker restart while model-free
Telegram commands/forms continue normally. No personal prompt, audio, upstream error
text or API key may enter the stored gate.

The runtime attaches a shared database gate to Gemini's request boundary. Quota errors
pause for 120 seconds, authorization/model errors for 30 minutes, other provider outages
for 60 seconds. A later success clears the state. Changing configured credentials or
model creates a new configuration fingerprint; plaintext credentials are not stored.
Consent is still checked before each request. Paused calls do not contact Gemini or
emit another quota notification. Starting a quota pause durably enqueues the existing hourly-deduplicated notice even when an offline form catches the error. The Telegram acknowledgement worker delivers this notice independently of analysis.

A dedicated nonblocking PostgreSQL advisory lock serializes provider requests across
processes using this database. It holds no transaction or diary/ingest lock during the
existing bounded network request. Busy requests defer briefly. The runtime schedules
retries at the stored deadline instead of immediately retrying each queued message.
Waiting on a shared pause does not consume job attempts. Explicit forms catch provider unavailability and use their existing offline path.

The gate is local to this instance/database, not a global account-wide quota meter.
Standalone construction of GeminiProvider without the runtime gate remains available
for isolated provider tests. This phase adds no provider SDK retries, cost estimates,
pending-inbox UI or new notification frequency. Synthetic tests cover restart, concurrency,
configuration changes, recovery and error classification; no live Gemini call is made.

A malformed persisted deadline or non-object gate does not create an endless retry
loop: the same provider lock permits one recovery request, whose success or failure
replaces the invalid state with a ready state or a finite fresh cooldown.

A shared cooldown overrides exponential retry backoff even after earlier failed attempts. Unclassified request-specific 4xx and local request-construction errors become ProviderRequestInvalid without opening the shared outage gate. Transport errors and 5xx still pause requests; deterministic form and oversized-message fallbacks handle request-local failures as well.

The request opening a provider pause uses the same finite retry deadline as later paused jobs, including after prior attempts. Invalid request errors are terminal on the first attempt and use the existing failed-inbox notice; they do not pause unrelated work. The durable gate is the sole quota-notice producer. A lost connection during lock cleanup cannot replace the provider response or error.
