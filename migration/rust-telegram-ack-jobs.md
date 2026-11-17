# R27 Telegram callback acknowledgement jobs

Native acknowledgements use the existing job table and lease protocol. Their dedicated job kind carries the authenticated bot instance, owner and callback identifier. An ingress transaction can enqueue the acknowledgement alongside its update. A separate control worker can claim it during a long poll. The owner, bot and update identity prevent duplicate jobs, and a lost lease can be reclaimed after restart.

The disposable TimescaleDB test covers rollback, duplicate updates, per-bot isolation, competing claims, expired leases and delayed retry. It also checks that the prior Python acknowledgement job stays available to its own consumer. The Linux arm64 test peaked at 7,823,360 bytes under a 32 MiB memory limit, with zero swap and no OOM events.
