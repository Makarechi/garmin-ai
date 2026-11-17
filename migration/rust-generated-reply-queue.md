# R25 Generated reply queue fences

The native queue publishes a generated answer only while its authenticated inbound revision, target memory epoch and complete set of opted-in source epochs match. It stores the result and matching fence atomically with any single-use actions. The deduplication key uses the same sorted JSON digest and revision as Python, so a restarted attempt returns the original outbox row.

Seven paired Python and Rust outcomes match, including a repeated result, stale target, missing or stale sources and an edit before publication. A separate fault injection confirms that a failed fence write rolls back its outbox row and signed action while allowing unrelated outer work to commit. The Linux arm64 test peaked at 9,150,464 bytes with zero swap under a 32 MiB cap.
