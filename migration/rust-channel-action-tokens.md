# Native channel action tokens

Rust can issue and consume the same signed action references as Python. It reuses the existing per-owner signing key, checks the authenticated owner, conversation, revision and expiry, and stores only a hash of each token in the lookup record. The caller issues a token when writing the corresponding outbox intent and commits consumption with the confirmed action. The owner and token row locks keep concurrent attempts from consuming the same action twice. Failed action transactions can roll back consumption.

The pinned Python implementation seeded the disposable database with five synthetic tokens. Native Rust verified the existing tokens, rejected wrong context, tampering, expiry and malformed storage, and allowed one of two concurrent consumers. It also verified rollback, fresh signing key creation and the erasure fence. Pinned Python accepted a newly issued native token exactly once. The Linux ARM64 run reached 7,503,872 bytes in a 32 MiB container with no swap.

These database operations take their context from an authenticated caller. The Telegram callback adapter and outbox still need to supply the current message revision and commit the resulting action.
