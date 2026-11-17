# Native provider outcome persistence

The native gateway uses the same PostgreSQL session lock as the pinned
Python gateway. Different provider instances retain independent locks.
The dedicated session holds no database transaction across a request and
releases its lock on drop. Short transactions read or record gateway state.

A recorded result preserves a later scheduler pause for the same
configuration. A configured Gemini quota notice and its state commit
together, with at most one notice per hour. The state and notices remain
free of prompt and response content.

Eleven synthetic outcomes matched Python's stored state and notice
decisions, including existing offsets, a legacy Gemini instance,
credential rotation and repeated quota results. A competing PostgreSQL
session verified the same lock, and a forced write failure rolled back
the notice before a successful retry. The Linux ARM64 fixture peaked at
10,113,024 bytes in a 32 MiB container with no swap.
