# Optional calendar plans (GA-30, import contract)

Completion criteria for this phase: explicit source/category opt-in; no meeting titles,
third-party names or exact location; idempotent ordered revisions and cancellation;
no diary facts created from plans; disabled operation remains available.

`GA_CALENDAR_SOURCES` is an empty JSON list by default. Each enabled entry has an opaque
UUID `id`, selected `categories` (`work`, `personal`, `travel`, `exercise`, `other`) and
an aware `granted_at`. Compose forwards the same setting to the API container. Removing a source or category immediately excludes its stored
plans from reads and future imports. Revocation does not delete retained state.

Admin-only `POST /context/calendar/import` accepts `{ "items": [...] }` with at most
100 records. A busy record contains only opaque `source_id`/`id` UUIDs, a monotonically
increasing integer `revision`, `status: "busy"`, aware `start`/`end`, IANA `timezone`
and a coarse `category`. The adapter must map vendor IDs to stable opaque IDs and
source changes to ordered revisions. A cancellation contains only the two IDs,
revision and `status: "cancelled"`. Reused revisions with changed content conflict;
older revisions cannot revive cancellations. Retained historical revision hashes also reject changed-content reuse; revisions older than the retained 20 hashes are only classified as stale. A batch is atomic.

Admin-only `GET /context/calendar?start=...&end=...` returns overlapping plans in a
half-open range of at most 31 days. UTC intervals retain the original named timezone.
The response labels a busy interval as a plan, never attendance, activity or stress.
Missing plans do not prove free time. This data is not registered as a shared agent
or MCP tool and is not transmitted to a language model.

Storage is capped at 2,000 source records, including cancellation tombstones. Updates
retain the current record plus the last 20 revision hashes, not a full historical
calendar archive. At capacity, existing records can still be corrected or cancelled;
new IDs are rejected explicitly. Full encrypted database backup and erasure include
this AppState namespace. The diary export does not include calendar plans.

This phase performs no external network request, calendar mutation or OAuth setup.
Weather history, reviewed provider/license selection, incremental adapters, retention,
scoped connector credentials and explicit confounder AnalysisSpec integration remain
separate phases. All verification uses synthetic calendars.
