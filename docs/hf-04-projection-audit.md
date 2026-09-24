# HF-04: preview custom projection drift

Run `garmin-ai projection-audit --limit 500` against the local database. The command
returns counts of missing, stale, and mismatched *current* metric observations and a
cursor for the next page. It does not print recorded values or modify events, audit,
or observations. `history_unknown` counts events whose audit snapshots lack explicit
fact status; those histories cannot be backdated safely from the current row.

This is the read-only planning step for historical reprojection. No repair is
performed by this command. A later repair must use verified audit transition times,
preserve prior knowledge cutoffs, and separately validate changes before applying.
