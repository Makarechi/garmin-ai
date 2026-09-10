# User diary export (GA-29, first phase)

Authenticated `GET /exports/diary` accepts timezone-aware `start` and `end`, optional
display `timezone`, and `format=json|csv`. It requires `read:diary`; health-only or
write-only access does not grant export permission. The endpoint does not contact a
model or Garmin.

The half-open window includes intersecting open episodes and preserves their original
start and missing end. JSON and CSV contain original timestamps/timezone, separately
converted display times, source, status, confidence, revision, topology and typed diary
payloads with their stored units. Missing observations remain unknown. Deleted events
are excluded. CSV uses quoted UTF-8 cells, JSON payloads and formula-prefix escaping;
use JSON when exact round-trip text is required.

The export is limited to 31 days, 1,000 rows and 2 MB of serialized JSON. Exceeding a
limit rejects the request rather than silently omitting records. The response is a
download with `Cache-Control: no-store`; saving/sharing the downloaded diary is the
owner's action. It contains personal diary facts, but excludes original Telegram
messages, idempotency keys, account bindings, credentials and operational tables.

This is separate from the administrative backup/export command, which has a different
scope. Dashboard, coverage heatmaps, evidence cards and Telegram download controls
remain subsequent GA-29 phases. Tests use only synthetic diary and credential markers.
