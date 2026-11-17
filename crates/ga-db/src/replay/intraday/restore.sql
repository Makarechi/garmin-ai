INSERT INTO measurements (
    ts, metric, source, local_date, value, unit, metric_definition_version_id,
    source_ref, quality, details, ingested_at
)
SELECT target.ts, target.metric, target.source, target.restored_day,
       target.restored_value, target.restored_unit, version.id,
       $1, 'observed', '{}'::jsonb, target.restored_at
FROM pg_temp.ga_replay_sample_targets AS target
JOIN jsonb_to_recordset($2::jsonb) AS version(metric text, id uuid) USING(metric)
WHERE target.restored_at IS NOT NULL
