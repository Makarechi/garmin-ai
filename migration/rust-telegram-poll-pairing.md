# Confirmed pairing for Telegram polling

The transport checks the current pairing before a provider request and again in the transaction that accepts each response update. A pairing change during a long poll prevents that update, its callback acknowledgement and cursor movement from being committed. The worker clears its recent-poll marker after a failed poll. The webhook and polling paths share the same pairing check.

The loopback test drained a full backlog, acknowledged a callback independently, then rotated the pairing while the next request was in progress. The uncorrected worker accepted the stale result. The corrected worker rejected it, kept the previous offset, and made no further request under the old pairing. The Linux ARM64 test peaked at 13,205,504 bytes with a 32 MiB limit and no swap or OOM events.
