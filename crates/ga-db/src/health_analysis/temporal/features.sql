WITH eligible AS MATERIALIZED (
    SELECT id, source_ref, metric, value, observed_at, fetched_at, ingested_at, sequence,
           effective_start, timezone, source_calendar_date
    FROM metric_observations
    WHERE metric = ANY($1::text[])
      AND observed_at >= $3::timestamptz - interval '24 hours'
      AND observed_at < $4::timestamptz AND ingested_at <= $5
      AND quality = 'observed' AND feature_version = $6
    LIMIT 100001
), budget AS MATERIALIZED (
    SELECT count(*) <= 100000 AS within_limit FROM eligible
), preferred AS MATERIALIZED (
    SELECT DISTINCT ON (metric, observed_at) *
    FROM eligible, budget
    WHERE budget.within_limit
    ORDER BY metric, observed_at, fetched_at DESC, ingested_at DESC, sequence DESC
), points AS (
    SELECT metric, observed_at AS clock, observed_at AS evidence_at, false AS event
    FROM preferred
    UNION ALL
    SELECT metric, clock, NULL, true
    FROM unnest($1::text[]) AS metrics(metric)
    CROSS JOIN unnest($2::timestamptz[]) AS events(clock), budget
    WHERE budget.within_limit
), history AS (
    SELECT metric, clock, event, max(evidence_at) OVER (
        PARTITION BY metric ORDER BY clock, event DESC ROWS UNBOUNDED PRECEDING
    ) AS evidence_at
    FROM points
)
SELECT history.clock, history.metric, preferred.value, preferred.id, preferred.source_ref,
       preferred.observed_at, preferred.ingested_at, preferred.effective_start, preferred.timezone,
       preferred.source_calendar_date
FROM history
LEFT JOIN preferred ON preferred.metric = history.metric
    AND preferred.observed_at = history.evidence_at
    AND preferred.observed_at >= history.clock - interval '24 hours'
WHERE history.event
UNION ALL
SELECT NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL FROM budget WHERE NOT within_limit
