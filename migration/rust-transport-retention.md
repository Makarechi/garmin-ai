# R33 transport text retention

The native `prune-telegram-text` command previews by default and applies only on explicit request. It uses the same storage and runtime locks as the Python command. The ordered cursors cover legacy updates, proactive answers, neutral inbound text and unlinked outbox text. Unresolved delivery and unfinished legacy jobs keep their source text. Redaction preserves diary facts, operation identity, deduplication and delivery receipts.

`migration/check_transport_retention.py` compared the native Linux ARM64 command with pinned Python `ee3a14a48a6ad30c1085951bac2f53c9088c2450` on separate disposable PostgreSQL databases. Five preview pages matched, including independent answer and neutral cursors. Apply and retry preserved the same records and tombstones. The command also refused while the original Python file or database runtime lock was held.
