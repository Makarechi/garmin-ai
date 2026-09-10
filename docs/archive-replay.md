# Offline archive replay (GA-08, canonical projection slice)

After a parser upgrade, the scheduler plans up to 25 raw-reference jobs per pass,
with at most 100 queued or running. Jobs are unique by raw reference and parser
version, survive restart, and run without Garmin tokens or network calls. The
enrolled account is checked again before writing. Current-version failures retain
the ordinary bounded job retries and remain visible in `data_freshness.archive_replay`.

Replay verifies SHA-256 against the retained file and uses the original source
request watermark. In particular, a current A→B→A correction retains the last A
watermark even though content-addressed A was first archived earlier. Superseded
revisions are recorded as skipped rather than overwriting current projections.
FIT uses the current activity archive pointer. Normalization and replay progress
commit together; errors retain raw data and only exception classes in diagnostics.
Existing candidate/accepted/delivered insights are superseded after reprocessing.

This slice rebuilds canonical projections using existing endpoint parsers, including
old readiness archives. It does not introduce an undocumented training-status
parser or reconstruct every superseded historical metric version. Schema drift
quarantine, endpoint capabilities and richer parser statuses remain separate GA-08
work. An archived-only endpoint remains archived-only after replay.

Synthetic tests cover two-year-old raw data, idempotence, A→B→A, hash mismatch,
account mismatch, bounded scheduling and insight invalidation. No live Garmin or
LLM requests are involved.
Insight claims, generation and delivery wait for the current parser replay, including
old raw rows not yet admitted by the bounded planner and failed replay jobs. Technical
replay status must be repaired before automatic insights resume. Replay supersedes
uncertain deliveries as well as accepted/delivered insights; their delivery logs remain.
Existing-owner normalization uses shared owner locks; only first enrollment is exclusive.

Canonical parser-version mismatches block analysis in both upgrade and rollback directions. Completed older-target jobs are requeued if their canonical source was subsequently projected by a different version. Superseded raw revisions cannot establish current readiness. Interactive analysis and `/today` return an explicit replay notice while canonical projections are inconsistent.

Replay preserves per-source interpretation timezone recorded at ingestion. Legacy sources with one recorded observation timezone reuse it; previously normalized sources without recoverable timezone stop with a technical error instead of silently moving historical days. Sources never successfully interpreted use the configured timezone. New failed revisions advance request provenance; legacy failed revisions newer than the success watermark are preferred. Failed rollback jobs get one renewed retry cycle per observed projection version, preventing unlimited retries of the same broken projection. Unchanged post-commit retries do not invalidate newly generated outputs.
