# Schema, consent, and pack security

Definition and integration management are separate permissions. Provider output can propose a
schema but cannot authorize it. Schemas use a bounded, closed JSON profile: only local `$ref`
targets, finite numeric and collection limits, no callbacks, code hooks, remote resources, or
extension keywords.

Sensitive trackers require explicit per-destination consent before their schema or facts can be
shared with a model or channel. Consent identifies the tracker, destination instance, categories,
time, and policy revision; adding a tracker never broadens old consent.

Shareable packs contain definition versions, field metadata, translations, safe tracker settings,
metric contracts, and mappings. They contain no events, observations, original messages, owner IDs,
bindings, tokens, provider configuration, or secrets. Full encrypted backups remain the mechanism
for restoring facts and complete version lineage.

Action references are signed, expiring, bound to owner, conversation, and revision, and consumed
once. Labels are presentation only and never authorize an operation.
