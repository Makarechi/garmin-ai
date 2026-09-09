# Fetch, observations and coverage (GA-03)

`data_freshness` separates endpoint attempts (`fetched_at`, request start time),
`last_success_at` (successful API response), observation recency and coverage. Parser
errors are separate from fetch errors. An endpoint failure preserves the preceding
success timestamp; a first failure has no invented success. Older attempts cannot
replace newer endpoint state. Legacy `success_at` and `lag_seconds` remain readable.
Raw source references expose source revision hash and parser version; upstream
`source_updated_at` stays null when no trustworthy timestamp was supplied.

`channels` reports last valid observation, expected interval, observed interval,
coverage, recent-window coverage and a `usable_for_current_state` gate. Fetch and
observation source references are distinct, including when an empty new source
coexists with valid older history. Future and non-observed quality samples do not
establish trusted recency. Old last observations remain visible even outside the
two-day coverage query.

Coverage is elapsed duration between adjacent distinct samples, never point count.
The engineering policies join gaps up to five minutes for HR/stress/respiration,
15 minutes for Body Battery and one hour for hourly SpO2. These are conservative
calculation policies, not claims about vendor cadence, continuous wear or diagnosis.
No interval after the newest sample or across a longer gap is filled. Coverage uses
UTC duration and configured local midnight, including DST. Recent-state use requires
80% coverage over the last 30 minutes for HR/stress/respiration, one hour for Body
Battery or two hours for SpO2, as well as an observation inside that window.

Daily sleep/HRV summaries use calendar recency (today or yesterday); readiness uses
today. Daily summaries have no fabricated timestamp or coverage percentage and
cannot establish a current instantaneous state. Versioned pre-event readiness is
separate work in GA-04. `/status` displays fetch time, HR observation lag/coverage and
the HRV summary date. The answer agent receives channel quality before its first
model call and must name lag/missing channels for current-state questions. Snapshot
tools explicitly label their daily semantics. This does not guarantee model wording.

Context questions additionally require valid observed-quality stress/HR and at least
80% HR coverage in their evidence window; clustered samples cannot pass a count-only
gate. Historical trend tests still use completed daily observations rather than a
live HR recency gate.

No schema migration or history rewrite is needed. Endpoint state expands compatibly
and coverage is derived at read time. `not_synced`, `unknown`, `partial`, `source_empty`,
`parser_error`, `fetch_error` and `stale_observation` are distinct. Device support,
sensor-disabled and non-wear causes are not inferred from gaps; explicit capability
evidence and durable account/source lifecycle are follow-ups in GA-07/GA-08.

Synthetic PostgreSQL regressions: `test_observation_freshness.py`.
