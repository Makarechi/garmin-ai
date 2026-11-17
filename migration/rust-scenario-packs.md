# Built-in scenario pack preferences

The native registry carries the six trusted packs exported from pinned
Python. It retains the fresh-install and legacy-install defaults and stores
tracking, collection, reminders, visibility and model access independently.
Revision checks keep stale editors from replacing newer preferences.

Revoking model access takes the existing policy and delivery locks. It
retires stored analysis context, advances conversation memory epochs and
clears the legacy pending state affected by the change. Reminders are
cancelled according to the pack rules in the same transaction.

`check_scenario_packs.py` compares both fresh and retained installations
between pinned Python and native Linux ARM64 on four disposable databases.
The fixtures compare all pack responses, recorded reminder states, the
conversation reset and old reply handling. They check permission denial,
stale revisions and the rollback when an in-flight delivery holds the context
lock. Import, export, forms and dispatch remain separate steps.
