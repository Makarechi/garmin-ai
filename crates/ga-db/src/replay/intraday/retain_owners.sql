WITH existing AS (
    SELECT ts, metric, source, source_ref,
           to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') ||
           CASE WHEN extract(microseconds FROM ts) % 1000000 = 0 THEN ''
                ELSE '.' || to_char(ts AT TIME ZONE 'UTC', 'US')
           END || '+00:00' AS stamp
    FROM pg_temp.ga_replay_sample_targets
)
INSERT INTO app_state(key,value)
SELECT concat('sample-owner:',stamp,':',metric,':',source),
       jsonb_build_object('ts',stamp,'metric',metric,'source',source,'source_ref',source_ref)
FROM existing
ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=now()
WHERE app_state.value IS DISTINCT FROM excluded.value
