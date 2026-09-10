# API and MCP least privilege (GA-26, access boundary)

MCP exposes reads by default and rejects direct calls to write tools before validating
arguments or opening a database transaction. Set `GA_MCP_ENABLE_WRITES=true` locally
and restart MCP to enable diary creation/correction/deletion. Annotations are only
hints; the execution boundary enforces the setting. Existing write-enabled integrations
must explicitly opt in after upgrading.

HTTP supports `GA_API_TOKENS`, a JSON array of objects with `key` and `scopes`. Keys
must be distinct, non-placeholder secrets of at least 32 characters. Generate random
keys locally; never put actual credentials in source control or command arguments.
The default scope for a scoped token is `read:health`. Supported scopes:

| Scope | Access |
|---|---|
| `read:health` | Garmin summaries, series, activities, baselines and health-only analyses |
| `read:diary` | Diary event reads |
| Both read scopes | Timeline, stored insights, event-window and migraine analyses |
| `read:diary` + `write:diary` | Diary mutations, including idempotent replay responses |
| `admin` | All above plus operations/metrics |

All mutations require diary read permission because responses can contain existing
record fields. `/tools` filters discovery; `/tools/{name}` independently checks the
same explicit policy. Unknown future tools are denied until assigned a policy.
Missing/invalid credentials return 401; valid credentials with insufficient scope
return 403. Telegram owner/webhook validation and minimal health endpoints retain
their separate authorization rules.

For compatibility, `GA_API_KEY` remains an explicitly administrative legacy key.
To remove its broad access, configure scoped tokens and clear `GA_API_KEY`, then
restart the API. For rotation, add a new distinct token, move clients to it, remove
the old token and restart. Removing a key revokes it after restart. Secrets remain
process configuration and are not included in database backup/export.

This PR covers the access portion of GA-26. Garmin account binding/enrollment is a
separate change; scoped credentials do not establish source-account identity.
Tests cover every tool policy, direct MCP bypass attempts, API discovery and direct
execution across scope sets, diary reads/mutations by ID, and invalid credentials.
