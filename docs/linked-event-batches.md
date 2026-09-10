# Linked diary drafts (GA-16, first phase)

The interpreter can explicitly link a newly reported medication to a new migraine
in the same message using typed draft_links (child_index and parent_index). No
UUID is invented by the model. The application validates the whole link graph,
creates parents first, resolves actual UUIDs and preserves input-order idempotency
keys. Existing UUID relations cannot be silently replaced by local links. Names,
doses and times retain the existing required validation.

All log drafts are created inside one savepoint: a failed child rolls back newly
created facts and audits, even if a caller catches the error and commits its outer
transaction. Retrying the same Telegram update resolves the same parent and does
not duplicate medications or audits. Returned confirmations preserve input order.

This phase supports medication-to-new-migraine links for log commands. Linking
symptom drafts, operation-level undo/redo, edited-message reconciliation and voice
confidence cards remain subsequent GA-16 phases. Existing undo still applies to
the last individual mutation; it is not yet a whole-batch undo.
