# Legacy Garmin activity summaries

A captured activity page or single summary updates the existing activity rows.
Nested summary fields take precedence. Missing or invalid optional numeric
values retain the installed value. Activity display timezones and names survive
partial responses, and a late page cannot replace a newer summary from another
endpoint. Activity detail documents and FIT columns stay attached.

Every changed or supplied summary field keeps its source reference. For rows
from before field ownership was stored, the update reads relevant archived
source rows and reconstructs ownership, favoring values that match the
installed activity when original timestamps cannot recover application order.
A bad page rolls back every row in that page and retains the indexed original
for retry. A single-activity response must match its requested identity.

The synthetic comparison writes the same sequence using pinned Python and a
native Linux ARM64 build on disposable TimescaleDB. It compares activity fields
and per-field ownership after recovering an old incomplete state map. It also
checks preservation of newer rows and complete rollback when a later row in
a page has an invalid start time.
