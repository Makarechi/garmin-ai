# Verified offline replay input

Before interpreting a raw source, Rust checks the requested parser version,
the recorded owner and the same canonical-source selection used by the
scheduler. It verifies the stored SHA-256 and file length in bounded chunks.
FIT inputs use the open verified file. JSON inputs are decoded within the
caller's byte limit with exact integer values.

Replay retains the time of a matching attempt, the time of the last retained
application, or the recorded legacy field and observation clocks as applicable.
The recorded interpretation timezone is preferred over current configuration.
Previously interpreted timed records with missing or inconsistent timezone
evidence are rejected. A saved interval replacement must have valid bounds,
channels and completeness evidence.

`check_replay_input.py` creates a disposable TimescaleDB and synthetic Python
archives for 26 reference decisions. The native Linux ARM64 test compares
source eligibility, parser version checks, applied times, timezone selection,
replacement rules, exact JSON and physical archive corruption under a 32 MiB
container limit. It separately checks owner denial and byte limits. No test
queries Garmin. Writing projections or finishing queue jobs belongs to the
next slice.
