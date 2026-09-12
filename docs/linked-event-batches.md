# Linked diary drafts (GA-16, first phase)

The interpreter can explicitly link a newly reported medication to a new migraine
in the same message using typed draft_links (child_index and parent_index). No
UUID is invented by the model. The application validates the whole link graph,
creates parents first, resolves actual UUIDs and preserves input-order idempotency
keys. Existing UUID relations cannot be silently replaced by local links. Medication
details can remain unknown for an explicitly reported intake; known doses and explicit times retain validation.

All log drafts are created inside one savepoint: a failed child rolls back newly
created facts and audits, even if a caller catches the error and commits its outer
transaction. Retrying the same Telegram update resolves the same parent and does
not duplicate medications or audits. Returned confirmations preserve input order.

This phase supports medication-to-new-migraine links for log commands. Linking
symptom drafts, redo, edited-message reconciliation and voice
confidence cards remain subsequent GA-16 phases. Operation-level undo for new
batches is described in operation-undo.md; legacy mutations retain individual undo.
