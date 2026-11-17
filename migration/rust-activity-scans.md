# Recent and history activity scans

The native scheduler stores recent and historical scan cursors alongside their
first jobs in one transaction. It keeps an active cursor after restart even
when a page has exhausted its retries, honors the saved next-scan time and
checks collection permission before scheduling. Recent and history scans
retain their existing lookback dates in the owner's timezone.

The page calculation compares the retained 20 activity identities with the
next page. A shift restarts the scan at offset zero and persistent shifts
defer it for an hour. A completed recent scan returns after 15 minutes and a
completed history scan returns the next day.

Native Linux ARM64 matched pinned Python in 25 page decisions and six
durable scheduling passes on two disposable TimescaleDB databases. They
include DST, dates on the edge of the requested history, overlapping IDs
above binary64 integer precision, a changed inventory, failed jobs, a saved
cooldown, changed collection permissions and a held mutation lock.
