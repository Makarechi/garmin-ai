# Native delivery receipts and recovery

Native callers can persist provider observations in the existing outbox transaction. Acceptance, confirmed delivery and confirmed reading stay distinct. A late observation remains evidence without changing the current outbox state, and a duplicate returns the existing evidence identifier. A receipt from a replaced lease or an unrelated owner is rejected.

Recovery moves expired sends into an uncertain state in batches of up to 100. A queued or sending observation cannot trigger a resend from that state. Reconciliation requires the verified owner and integration management permission. Provider evidence may still confirm the original send.

Fourteen synthetic receipt, recovery and requeue snapshots matched pinned Python. Native checks also covered foreign access, the uncertainty fence, and an injected transaction failure. The Linux ARM64 run peaked at 8,044,544 bytes in a 32 MiB container with no swap. Transport adapters still need to authenticate their observations and call these storage operations.
