# Channel contracts

`garmin_ai.channels` is the boundary between application behavior and a messaging
provider. Provider adapters authenticate input, normalize it into an
`InboundEnvelope`, and render `OutboundIntent` values using their capabilities and
the recipient-specific delivery policy.

External event, message, sender, attachment, and provider receipt identifiers are
opaque strings. Their identity always includes the channel and channel-instance
namespace. They must not be parsed as counters or substituted for internal UUIDs.

Capabilities describe what an adapter can represent. The delivery policy is
evaluated for each attempt because quiet hours, consent, recipient state, or
provider restrictions can change. A missing feature has a semantic fallback:

- actions become numbered choices with single-use tokens;
- edits become a new message related to the earlier message;
- replies become a clearly introduced related message;
- voice requests become text;
- an initiative that cannot be sent remains queued with a reason and optional
  retry time.

Delivery evidence is intentionally conservative. `provider_accepted` means only
that the provider accepted the send request. It does not mean delivered or read.
Those states require explicit provider evidence. A timeout after a send is
`uncertain`, not a reason to assume failure or blindly send through another
channel.

`InMemoryChannel` is a restrictive contract adapter. It exercises these fallbacks
without pretending that a second real messenger has been implemented.
