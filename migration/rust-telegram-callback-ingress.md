# Native Telegram callback ingress

Rust verifies that the button belongs to the authenticated private Telegram conversation and resolves it from the existing outbox. An expired or superseded button cannot create a new inbox action. Token consumption, removal of the saved token, and insertion of the neutral event commit together. A duplicate update returns the saved event even after the one-use token has been spent.

Pinned Python generated four synthetic buttons for the native database test. Rust accepted compatible buttons, rejected a different bot instance, a replay under another update identity, expiration and an older message revision, and recovered cleanly after a forced inbox write failure. The Linux ARM64 run used 8,282,112 bytes in a 32 MiB container without swap.
