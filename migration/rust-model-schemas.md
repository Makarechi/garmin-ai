# Native model response schemas

The compiler translates trusted application schema documents into the
closed-object subsets used by OpenAI and Anthropic. It preserves property
order in serialized maps, required keys and optional choices, resolves local definitions
and rejects constructs that cannot be represented. Anthropic's wire format
omits some constraints that the application still validates locally. Its
nullable simplification follows the original field order and grammar limits.

Linux ARM64 compared 52 provider decisions or complete schema documents
with the pinned Python compilers. All five approved application schemas
produced matching wire documents for both providers. An order-sensitive
test reproduced alphabetic reordering before insertion-ordered maps were
enabled. It now checks serialized property and definition maps at every
nesting level. The fixture also covered recursive and dangling references, open objects,
numeric bounds, and the nullable complexity boundaries.

This compiler accepts the caller's application contract. The outbound
request layer must supply a registered contract and keep it for validation.
