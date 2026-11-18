WITH episodes AS NOT MATERIALIZED (
    SELECT start,"end" FROM events
    WHERE kind='migraine' AND NOT deleted AND status='confirmed'
      AND start < $3 AND ("end" > $2 OR ("end" IS NULL AND topology='open_interval')
        OR (("end" IS NULL OR "end"=start) AND topology IN ('point','flexible') AND start >= $2))
), observations AS NOT MATERIALIZED (
    SELECT start,"end",kind,status,source,payload FROM events
    WHERE kind IN ('headache_observation','symptom_observation') AND NOT deleted
      AND start < $5 AND ("end" > $4 OR ("end" IS NULL AND topology='open_interval')
        OR (("end" IS NULL OR "end"=start) AND topology IN ('point','flexible') AND start >= $4))
), bounds AS (
    SELECT * FROM unnest($6::date[],$7::timestamptz[],$8::timestamptz[]) AS b(day,left_at,right_at)
)
SELECT b.day,
    CASE
        WHEN evidence.positive THEN 'positive'
        WHEN evidence.unknown THEN 'unknown'
        WHEN b.left_at >= b.right_at OR evidence.covered @> tstzrange(b.left_at,b.right_at,'[)')
            THEN 'confirmed_negative'
        WHEN evidence.symptoms THEN 'unknown'
        WHEN evidence.negatives THEN 'incomplete'
        ELSE 'unanswered'
    END AS coverage,
    EXISTS(SELECT FROM episodes e
        WHERE (e.start AT TIME ZONE $1)-interval '3 days' < (b.day+1)::timestamp
          AND (e."end" IS NULL OR (e."end" AT TIME ZONE $1)+interval '3 days' > b.day::timestamp)
    ) AS excluded,
    EXISTS(SELECT FROM episodes e WHERE (e.start AT TIME ZONE $1)::date=b.day) AS episode_day
FROM bounds b
CROSS JOIN LATERAL (
    SELECT
        bool_or((o.kind='symptom_observation' AND (
            CASE WHEN jsonb_typeof(o.payload->'severity')='number'
                THEN (o.payload->>'severity')::numeric > 0 ELSE false END
            OR o.payload->'aura'='true'::jsonb
            OR o.payload->'symptoms' NOT IN ('null','false','0','""','[]','{}')))
            OR o.payload->>'headache'='yes' OR o.payload->>'migraine'='yes') AS positive,
        bool_or((o.kind='symptom_observation'
            AND o.payload->'impact' NOT IN ('null','false','0','""','[]','{}'))
            OR (o.kind='headache_observation'
                AND (o.payload->>'headache'='unknown' OR o.payload->>'migraine'='unknown'))) AS unknown,
        bool_or(o.kind='symptom_observation') AS symptoms,
        bool_or(o.kind='headache_observation' AND o.status='confirmed' AND o.source<>'inferred') AS negatives,
        range_agg(tstzrange(o.start,o."end",'[)')) FILTER(
            WHERE o.kind='headache_observation' AND o.status='confirmed' AND o.source<>'inferred') AS covered
    FROM observations o
    WHERE o.start < b.right_at AND (o."end" > b.left_at
        OR ((o."end" IS NULL OR o."end"=o.start) AND o.start >= b.left_at))
) evidence
ORDER BY b.day
