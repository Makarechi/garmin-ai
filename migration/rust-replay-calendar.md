# Offline calendar projection replay

The calendar replay handler uses verified archives for daily summaries, sleep,
hydration, body battery, readiness and max metrics. It keeps the original
application clock and its recorded timezone. An older retained raw can refresh
a field it still owns without replacing a later field or sleep interval.
Values rejected by the new parser lose their current projection while keeping
their recorded owner. Rebuilt observations retain their original knowledge
clocks and timezones.

Effects, replay status, analysis invalidation and the parser version commit
together under the storage mutation fence. Verified archives with an invalid
hash are recorded as failed and cannot publish. A database error leaves the
old projection and the original version ready for a later retry.

`check_replay_calendar.py` compares eight operations on separate disposable
TimescaleDB databases using pinned Python and a native Linux ARM64 binary.
It includes the same sleep response reappearing after a correction, a newer
retained sleep interval, both missing fields and empty responses, and damaged
or superseded archive input. The test forces a SQL failure after old fields
were cleared, checks that the original state survived, and then retries
successfully. It also exercises cancellation before mutation.
