# Native diary deletion and undo

Deletion uses the expected revision and the stored definition's permissions.
It preserves the previous payload in the audit and returns only the deleted
status and revision when that definition does not permit reading the entry.
Linked symptoms and medication prevent deletion of their referenced migraine.

Undo restores the authenticated actor's latest operation. An operation group is
undone in reverse order and rolls back completely if any revision has moved.
Versions already in the audit stay attached. Audits predating the event envelope
use the original creation actor and timestamp when deriving the older envelope.
Entry payloads are checked against their recorded schema. Schema resolution has
network and file access disabled. Restored observations get a new metric
projection while the previous fact remains available as invalidated history.

The pinned Python fixture writes linked observations, a stale operation group,
a pre-envelope audit and a custom definition with restricted read and delete
permissions. The ARM64 Linux test reads them on disposable TimescaleDB. Python
then reads the writes and checks reverse group order, full rollback, the legacy
envelope and both metric versions. The earlier Python/Rust creation and
correction fixture also passes with this metric path. HTTP idempotency and
answer reconciliation remain part of the HTTP and proactive integration steps.
