# Durable Garmin connection state (GA-07, queue gate)

`integration:garmin` persists active, rate_limited, degraded and reauth_required
states, a UTC block deadline, failure count, last attempt and exception class.
Raw exception text, URLs, passwords and tokens are not stored. The existing
single Garmin worker checks this gate before restoring tokens or fetching identity.
Claims and scheduling also respect it, so a paused queue does not consume retries
or grow with every polling interval. Diary and other job kinds remain available.

The first SDK 429 exits the request loop. Retry-After is honored when the SDK
retains a response header (seconds or HTTP date, capped at seven days); otherwise
backoff grows from one minute to one hour with jitter. The pinned SDK may not
retain response headers, so exact vendor deadlines cannot always be recovered.
Transport circuit failures persist a cooldown of at least 15 minutes. Restarting
the process does not reset any of these deadlines. Parser errors remain scoped
to their existing job retries; capability/schema classification belongs to GA-08.

Successful local login or explicit existing-owner enrollment clears the gate.
The current local file-lock protocol still requires stopping the worker for token
replacement; hot reauthentication and atomic token-generation handoff are a
separate slice. No password or MFA collection is added to Telegram.

The pinned `garminconnect.client.Client._refresh_session` persists refreshed DI
tokens through `dump` when a token store was loaded. A synthetic contract test
verifies this normal path. Upstream suppresses persistence exceptions, so this
does not claim durability after a filesystem failure; that remains part of token
handoff work. No live login or rate-limit experiment was performed.
When Garmin is paused, dependent agent cycles can still be claimed. Proactive cycles
process diary questions without physiological context and retire without retrying the
paused feed. Insight cycles are consumed without sending new Garmin-derived claims;
future scheduled cycles resume after the connection recovers.

