# Native service webhook

The native `serve` command mounts `POST /telegram/webhook`. It reads the existing Telegram webhook secret, private chat and integration settings. Process settings override the local `.env` file. An explicit integration allowlist with no enabled Telegram entry disables ingress. Invalid or missing credentials fail before the handler reads the body or uses the database.

The service checks the stored owner, selected channel and confirmed chat on each delivery. It repeats the pairing check in the inbox transaction while holding the ownership lock. A changed or missing pairing cannot write a delivery. The webhook retains the normal health, readiness and scoped API routes, still ignores events from other chats, and leaves the long-poll offset untouched.

The native CLI HTTP suite and the ten response cases captured from pinned Python passed against a disposable TimescaleDB database. The CLI test checks duplicate delivery and an updated owner pairing while the service remains running. The direct transaction test also rejects a pairing that changed after resolution. Linux ARM64 used 16,236,544 bytes for the eight HTTP tests, and 11,264,000 bytes for the paired webhook test, under a 32 MiB limit without swap. This work serves the local endpoint and does not register an external webhook.
