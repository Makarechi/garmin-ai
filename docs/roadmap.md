# Project roadmap

This roadmap summarizes the supplied `GARMIN_AI_HANDOFF.md` as design context.
It does not mean that its tasks have already been executed or authorize access
to external accounts. Detailed choices should be validated when implemented.

## Intended direction

- Python backend and a Garmin adapter isolated from application logic.
- Local-first PostgreSQL/TimescaleDB storage plus an archive of original payloads
  and activity files.
- Telegram for everyday logging, questions, and useful follow-ups.
- Provider-independent LLM tools, with Gemini as the proposed first provider.
- Deterministic analytics that report evidence, sample size, and uncertainty.
- MCP tools backed by the project's stored data.

## Incremental milestones

1. **Repository setup:** private GitHub repository, `main`, and a verified PR
   workflow. This is the scope of the initial setup.
2. **Garmin coverage investigation:** examine the current upstream library;
   once account access is available, verify actual data coverage, document the
   endpoint matrix, and prepare redacted fixtures. Final schema design follows
   the evidence gathered here.
3. **Durable ingestion:** authentication, raw archive, canonical storage,
   incremental synchronization, reconciliation, and freshness monitoring.
4. **Telegram diary:** private access, structured events, corrections, voice
   input, and durable symptom follow-ups.
5. **Question answering:** typed tools over stored data with evidence-aware
   answers and replaceable LLM providers.
6. **Proactive analysis:** useful follow-ups, personal baselines, activity
   comparisons, and cautious observational insights.
7. **MCP and optional context:** expose stored data to Codex; add contextual
   sources when useful.

Each milestone should be split into reviewable PRs. A milestone is complete
only after its behavior is verified, not merely after files are created.

## Current state

Only repository setup is implemented. No Garmin account has been connected,
no health data has been downloaded, and no application service is running.
