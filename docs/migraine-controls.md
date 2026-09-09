# Migraine control eligibility (GA-02)

`migraine-controls-v2` compares episode onset days with explicitly observed
headache/migraine-free control days. Matching remains deterministic, with no control
reuse: maximum cardinality, same weekday, within 56 days, minimum total distance.

Controls exclude the entire recorded migraine interval plus three calendar days
before/after. Unclosed episodes exclude every later candidate through the analysis
window, including episodes that started before the expanded search range.

`headache_observation` is a diary payload with separate `headache` and `migraine`
values (`yes`, `no`, `unknown`). It requires a nonempty start/end interval. The existing
event envelope preserves source, creation time, timezone, confirmation status,
revision and audit. These observations use the existing API/MCP event mutations and
Telegram structured logging; they are not synthesized from missing diary entries.
An explicit migraine without headache is representable.

Only confirmed observations from a non-inferred source establish negative coverage.
Adjacent negative intervals can cover a full local day, including 23/25-hour days.
Positive or unknown overlapping observations veto confirmed-negative classification;
conflicting records should be corrected with normal revision/undo operations.
Partial, unknown and unanswered days remain separately identified in `control_days`.
Exploratory candidates are listed but never used as confirmed matched controls.

Fewer than ten pairs yields `insufficient_evidence`. Even with ten pairs, output is
descriptive and subject to confounding; it does not support treatment recommendations.
`episodes` counts records; `episode_start_days` counts unique onset days. The matching
unit remains a day, so multiple starts on the same day are not independent samples.

Comparisons recompute directly from current rows. Changes to migraine/observation
events (create, edit, delete, undo) supersede stored migraine-category insights and
insights naming the migraine comparison tool. There is no persisted cohort cache.

No table migration is needed; existing backups and event audit remain readable.
Older application versions cannot edit the new payload kind: retain this version
when using new observations, or export them before an application downgrade.
Synthetic integration coverage is in `test_migraine_controls.py`; legacy matching
tests now explicitly create negative control observations and closed episodes.
