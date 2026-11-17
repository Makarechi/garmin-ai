# Durable Telegram reply delivery

The adapter sends a claimed neutral reply through the native Telegram transport after the outbox verifies the current lease, inbound revision, expiry and tracker sharing requirements. It records the provider message identifier only when Telegram returns one. Confirmation from Telegram records provider acceptance and leaves delivery and read receipts to channels that can observe them.

An explicit rate limit before the first part sets the next attempt time from the provider delay. A rate limit or transport failure after Telegram accepted a part records an uncertain outcome with the last known message identifier. This avoids automatically resending text that may already have reached the owner. Unsupported attachments stay queued without a network request, using the existing fallback delay.

The integration test uses a disposable TimescaleDB database and local HTTPS service. It verifies all of these outcomes, including stale claim rejection before a second attempt. Linux ARM64 peaked at 13,111,296 bytes under a 32 MiB limit with no swap. This adapter is available for the runtime worker and does not configure a live bot.
