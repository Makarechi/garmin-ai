WITH parsed AS (
    SELECT DISTINCT ON(ts, metric) ts, metric, value, unit
    FROM jsonb_to_recordset($1::jsonb) AS record(
        ts timestamptz, metric text, value float8, unit text, offset_in_body integer
    )
    ORDER BY ts, metric, offset_in_body DESC
)
UPDATE pg_temp.ga_replay_sample_targets AS target
SET unknown_order=false,
    restored_day=(parsed.ts AT TIME ZONE $2)::date,
    restored_value=parsed.value,
    restored_unit=parsed.unit,
    restored_at=$3
FROM parsed
WHERE target.ts=parsed.ts AND target.metric=parsed.metric AND target.source=$4
