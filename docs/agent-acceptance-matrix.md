# Agent acceptance matrix (scaffold)

The catalog at `tests/evals/acceptance_cases.json` lists all 40 planned system
scenarios. Their presence is not evidence that they have passed. The matrix
covers the planned 12 languages, three providers and five workflows; it does
not imply that any model or language is supported or accepted.

`tests/evals/acceptance_matrix.py` summarizes supplied result metadata. Each
record identifies one scenario, exact model, language, workflow, evidence level,
outcome, observation time, source commit and prompt, response schema and tool
specification SHA-256 digests. `synthetic` means a simulated provider; `live`
means a real API call using fictional inputs. Results are grouped by exact model
and level. A skipped live run is never counted as a live pass. The tool reports
recorded outcomes by case but does not assess acceptance or verify that a supplied
record accurately describes a run. Human review and the remaining deterministic,
ingress, safety and operational checks remain necessary.

Only synthetic test material is permitted. Do not put API keys, personal data,
health text, prompts, model outputs or error messages in metadata. The format
rejects unrecognized fields and uses a fixed set of failure reasons. Store the
metadata separately as an evidence artifact, and retain sanitized case-level
results and evaluation methodology under the project's review process.
Counts are counts of supplied records, not independently verified runs; do not
include duplicate records.

The metadata input is a JSON array. Every record has these fields:

```json
{
  "case_id": "AT-01",
  "provider": "openai",
  "model": "fictional-model-v1",
  "language": "sk",
  "workflow": "clarify_time",
  "level": "synthetic",
  "outcome": "skipped",
  "reason": "not_configured",
  "observed_at": "2026-01-02T12:00:00Z",
  "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "prompt_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "schema_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "tools_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
}
```

This example is illustrative and is not evidence of an actual run. Outcomes
are `pass`, `fail` or `skipped`; `reason` is null for a pass and otherwise one
of `not_configured`, `unsupported`, `provider_error`, `validation_failed` or
`other`. Use an observation timestamp with a timezone and actual digests from
the evaluated revision.

Example empty report:

```sh
printf '[]\n' > /tmp/garmin-eval-empty.json
python tests/evals/acceptance_matrix.py /tmp/garmin-eval-empty.json
```

No live evaluation, rollout gate or multilingual acceptance is provided by this
scaffold. Future runners must produce verifiable records and include semantic
review, held-out inputs, safety failures, latency and cost before release claims.
