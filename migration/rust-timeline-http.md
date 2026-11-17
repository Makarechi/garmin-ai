# Native diary and timeline tools over HTTP

Authorized diary clients can discover and read the owner event tool.
A layered timeline requires both diary and health scopes. The timeline
request keeps the caller's timestamp offsets and reads its layers from
the same replay-verified snapshot. Diary entries remain available during
Garmin replay.

A local TimescaleDB comparison matched 107 HTTP responses from pinned
Python across diary, timeline and the existing health tools. It verified
discovery, scope denial, boundary offsets, numeric timestamps, missing or
invalid filters and availability during replay. The layered timeline and
memory-limit fixture also passed at 32 MiB on Linux ARM64 with no swap.
