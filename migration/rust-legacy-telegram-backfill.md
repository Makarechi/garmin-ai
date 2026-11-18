# Earliest native Telegram history repair

The Rust migrator accepts `f103aa712b44`, the original neutral-message revision. It recovers the private Telegram conversation using the confirmed binding, the first historical update, or the legacy fallback. It moves both directions of message history and restores voice attachments, captions, and content types before the later identity repair.

Four Linux ARM64 cases matched pinned Alembic on separate disposable PostgreSQL databases. They exercised binding precedence, chat identity from a message or callback, the unbound fallback, caption versus text, and explicit/default voice content types. The comparisons included conversation state, inbound messages and outbound replies.
