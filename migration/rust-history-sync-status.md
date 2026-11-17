# Rust migration – history sync status

History status reads the installed windows, jobs, plans and activity scans
from one database snapshot. It reports missing or failed work and the
earliest completed nonempty window. Account identities and overlap
anchors remain private. Scan and plan entries are ordered by their
stored keys.

A native Linux ARM64 build matched the empty status and 7,300 synthetic
windows against pinned Python in TimescaleDB. The fixture included
running, failed and missing jobs, a superseded window with retained
source history, and multiple saved planning horizons.
