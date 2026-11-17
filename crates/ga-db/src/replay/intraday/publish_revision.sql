WITH matched AS MATERIALIZED (
    SELECT target, current
    FROM pg_temp.ga_replay_sample_targets AS target
    LEFT JOIN measurements AS current USING(ts,metric,source)
), watermark AS (
    SELECT greatest($1::timestamptz,clock_timestamp(),
         max((matched.target).ingested_at), max((matched.current).ingested_at)
    ) + interval '1 microsecond' AS rebuilt_at FROM matched
)
INSERT INTO measurement_revisions (
    id, ts, metric, source, local_date, value, unit, metric_definition_version_id,
    source_ref, quality, details, ingested_at, deleted
)
SELECT gen_random_uuid(), (target).ts, (target).metric, (target).source,
       COALESCE((current).local_date, (target).local_date),
       COALESCE((current).value, (target).value),
       COALESCE((current).unit, (target).unit),
       CASE WHEN (current).ts IS NOT NULL THEN (current).metric_definition_version_id
            ELSE (target).metric_definition_version_id END,
       CASE WHEN (current).ts IS NOT NULL THEN (current).source_ref ELSE (target).source_ref END,
       COALESCE((current).quality, (target).quality),
       COALESCE((current).details, (target).details),
       rebuilt_at,
       (current).ts IS NULL
FROM matched CROSS JOIN watermark
ON CONFLICT (ts,metric,source,source_ref,ingested_at,deleted) DO NOTHING
