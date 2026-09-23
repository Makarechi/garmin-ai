# Channel adapter guide

A channel adapter authenticates provider input and converts it to `InboundEnvelope`. It accepts
`OutboundIntent`, declares its real `ChannelCapabilities`, applies delivery policy immediately
before sending, and returns only the delivery evidence it actually observed.

External identifiers stay opaque and are always namespaced by the configured channel instance.
Adapters without buttons render actions as numbered text choices with single-use tokens bound to
the owner and conversation. Unsupported voice, edit, reply, attachment, or initiative behavior is
reported explicitly; it must not be silently dropped or simulated.

Provider SDK imports belong inside the adapter factory. Core startup, API, MCP, manual diary, and
analytics must work when that SDK and its credentials are absent. Configuration uses a stable
instance ID; secrets are never part of event definitions, templates, exports, or logs.

The restricted test adapter in `garmin_ai.restricted_channel` is the compatibility reference. It
has text only, uses opaque IDs, and emits delivery confirmation asynchronously. It is deliberately
not a real external integration and requires no webhook, credentials, deployment, or pricing
configuration.
