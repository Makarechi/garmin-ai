# Native outbox queue and action issuance

An authenticated caller can queue an outbound intent in the same transaction as the domain change it describes. Rust verifies the owner, conversation, inbound operation and tracker permissions. A repeated operation returns the first stored outbox entry. Action tokens use the corresponding inbound revision and are issued only once.

The queue operation uses a savepoint, so a later failure rolls back the outbox entry and any partially issued tokens even if the caller commits unrelated work. External files and replies are represented only by the stored neutral contract.

Seven synthetic enqueue and stored-state cases matched pinned Python. The native test also confirmed that a button signed for an edited message cannot be used with the earlier revision. Linux ARM64 used 7,483,392 bytes in a 32 MiB container without swap.

Explicit sharing requirements must be representable by canonical definition evidence on the persisted intent. Channel evidence represents the current version and the schema and facts categories. Requests missing matching evidence, naming a historical version or requiring original text are refused. This allows either runtime to cancel queued delivery when sharing is revoked.
