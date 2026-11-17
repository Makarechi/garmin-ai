# Native replay of Garmin activity summaries

The native store restores a single activity or page using its verified
archive. Recovery retains newer timing and fields from another source.
It restores still-owned fields, clears owned optional values omitted by
the saved response and preserves legacy field ownership when a historical
application time has been lost. The page and its metadata commit together.

Eight synthetic TimescaleDB cases matched the pinned Python activity
rows, field owners, raw status and replay outcomes. Cases covered a
drifted database copy, retained fields, missing historical timezone,
empty pages, a single invalid item and a forced write failure followed
by a successful retry. The existing live activity summary test also
passed in a 32 MiB Linux ARM64 container with no swap.
