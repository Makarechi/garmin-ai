# Activity history generations (GA-06)

After account enrollment the scheduler maintains separate recent (14-day) and
configured-history scans. One active scan per lane coalesces polling. Completed
recent scans repeat after 15 minutes; history scans after one day. History child
jobs retain backfill priority. A disabled history horizon leaves recent scans active.

Each persisted scan has an account, generation, lower date boundary, page offset,
overlap anchor and restart round. Pages overlap by 20 records. A changed anchor
detects shifted offset pages and restarts from the first page, at most three times.
An unstable inventory remains explicitly incomplete and can be scanned again after
one hour. Activity identities and generation-scoped child job keys prevent duplicates
while allowing a later same-day generation to fetch corrections.

Cursor advancement and child enqueue commit together. Retrying a page whose cursor
already advanced is a no-op; a process crash resumes the persisted queued page.
Exhausted jobs remain visible as failed rather than being declared complete.

This is a bounded scan of a mutable API, not a vendor snapshot or proof of deletion.
An empty page never proves account creation date. No tombstones are inferred from
missing records. Account-export ZIP import is a separate adapter still to implement.
Synthetic tests cover 250 records, insertion between pages, restart, deduplication,
bounded instability and separate recent/history scheduling; no live requests were made.
