# Native replay of Garmin activity details

The native store can rebuild details, splits, typed splits, heart-rate zones
and weather from the verified raw archive. Replay retains a previous part
after an empty response and leaves parts from a newer source intact. Source
status, parser version, provenance and the restored parts commit together.
The current runtime still needs to dispatch activity and FIT replay jobs.

A disposable TimescaleDB fixture compared seven outcomes with the pinned
Python application. It restored from the archive even when the database's
copy had drifted, preserved other parts, rejected a corrupt archive and kept
a missing activity's archive without creating a summary. A forced database
write failure left the previous part and parser version in place. A retry
then restored the original document.
