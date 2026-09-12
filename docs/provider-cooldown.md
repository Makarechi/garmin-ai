# Durable model cooldown (GA-32)

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
