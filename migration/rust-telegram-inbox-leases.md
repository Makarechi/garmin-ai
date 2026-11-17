# Processing leases for native Telegram messages

The native worker claims the earliest pending message in one owner and bot namespace. It uses the stored ordering epoch and provider update number, so a reset after a long gap cannot put new work ahead of existing work. The native queue records each accepted edit for ordering without updating the legacy last-new-message timestamp. An earlier delayed or actively leased delivery holds later diary work. A restart can claim expired work without giving a former worker permission to finish it.

Superseded message revisions are retired before a handler receives them. A provider cooldown sets the exact next attempt time and returns the unspent attempt. A message whose retries are exhausted becomes invalid so the inbox retention path can process it. The durable job remains the ownership record.

A disposable TimescaleDB run exercised two competing workers, a nine-day ID reset, expiry and restart, a revised message, a deferred retry, exhaustion and two successive resets whose first delivery was an edit. Before the fix the lower-numbered edit could be leased while older work was active. After the fix each edit waited for the older job. The Linux ARM64 test peaked at 7,528,448 bytes under 32 MiB with no swap. The lease path does not call a model or apply diary changes by itself.
