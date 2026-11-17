# Atomic diary replies for Telegram text

The native worker can commit a self-contained diary log from a leased Telegram text message. It uses the exact saved message and timestamp, verifies that the chat is still paired and selected, and rejects expired leases and older revisions. The diary writer validates and stores the records, projects the configured metrics, and queues a confirmation for the same conversation. All of that work and the inbox completion commit together.

This entry point accepts sufficiently confident log commands. Medication reports require the dedicated intake-evidence path. Reports pointing to existing records need current offered context. An edit of a message whose diary operation was already recorded needs that context to prevent an unintended second write. The later worker integration will supply the confirmation text and handle clarifications.

The disposable database test rejected a changed pairing, a timezone conflict, a medication without intake evidence, and an unknown record reference. It forced the outbox insert to fail, verified rollback of all records, metrics and audit entries, retried successfully and verified that the old lease and a later edit could not write again. Linux ARM64 peaked at 14,512,128 bytes in a 32 MiB container with no swap.
