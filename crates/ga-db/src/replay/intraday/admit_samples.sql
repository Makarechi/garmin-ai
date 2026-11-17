WITH incoming AS (
    SELECT ordinal, ts, metric, owner_key
    FROM jsonb_to_recordset($1::jsonb) AS row(
        ordinal integer, ts timestamptz, metric text, owner_key text
    )
), proofs AS (
    SELECT start, end_time, metrics
    FROM jsonb_to_recordset($4::jsonb) AS proof(start timestamptz,end_time timestamptz,metrics text[])
)
SELECT candidate.ordinal
FROM incoming AS candidate
LEFT JOIN pg_temp.ga_replay_sample_targets AS target
  ON (target.ts,target.metric,target.source)=(candidate.ts,candidate.metric,$5)
LEFT JOIN measurements AS current
  ON (current.ts,current.metric,current.source)=(candidate.ts,candidate.metric,$5)
LEFT JOIN app_state AS tombstone ON tombstone.key=candidate.owner_key
CROSS JOIN LATERAL (
    SELECT CASE WHEN current.ts IS NOT NULL THEN current.source_ref::text
                ELSE tombstone.value->>'source_ref' END AS id
) AS owner
WHERE target.ts IS NOT NULL OR (
    (
        NULLIF(owner.id,'') IS NULL OR owner.id=$2
        OR COALESCE(($3::jsonb->>owner.id)::integer,2147483647)
           < COALESCE(($3::jsonb->>$2)::integer,-1)
    )
    AND NOT EXISTS (
        SELECT 1 FROM proofs
        WHERE candidate.metric=ANY(proofs.metrics)
          AND candidate.ts >= proofs.start AND candidate.ts < proofs.end_time
    )
)
ORDER BY candidate.ordinal
