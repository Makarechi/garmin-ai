# Versioned custom definitions

The native registry supports drafting, proposing, activating and retiring a
custom definition under an authenticated owner with definition-management
permission. Revisions reject stale updates. Stable field identities are
checked across every stored version. Activation in a configured tracker
writes new metric mappings in the same transaction as the event definition
version. Existing versions and mappings remain usable. Retiring an entry
definition cancels its pending, sending and uncertain tracker reminders.

Tracker activation validates resolved field domains. Combined schema
alternatives have a bounded traversal to keep activation finite. A rejected
mapping leaves the previous active version, metric versions and proposed draft
intact.

`check_custom_definition_registry.py` initializes separate disposable
TimescaleDB instances with the same synthetic tracker written by pinned Python.
It compares an activation, a deliberately invalid activation, a revision with
a changed ordinal scale and category domain, retirement and a fresh definition
across Python and native Linux ARM64. It compares persisted contract hashes,
JSON draft state, all old and new versioned mappings and reminder states.
The native test also checks permission denial, stale revisions, duplicate
keys, stable field identities and rollback after a failed metric binding.
