WITH requested AS MATERIALIZED (
    SELECT state.value,
           CASE WHEN state.value->>'status' = 'pending'
                THEN COALESCE(job.status, 'needs_attention')
                ELSE state.value->>'status'
           END AS job_state
    FROM app_state AS state
    LEFT JOIN jobs AS job ON job.id::text = state.value->>'job_id'
    WHERE starts_with(state.key, 'syncwindow:')
), counts AS (
    SELECT COALESCE(job_state, 'null') AS status, count(*) AS n
    FROM requested GROUP BY job_state
), scans AS (
    SELECT state.key,
           (state.value - ARRAY['account','anchor','scan_key']::text[]) ||
           CASE WHEN state.value->>'status' = 'running'
                THEN jsonb_build_object('job_status', COALESCE(job.status, 'missing'))
                ELSE '{}'::jsonb END AS summary
    FROM app_state AS state
    LEFT JOIN jobs AS job ON job.id::text = state.value->>'job_id'
    WHERE starts_with(state.key, 'activity_scan:')
)
SELECT jsonb_build_object(
    'activity_scans', COALESCE((SELECT jsonb_agg(summary ORDER BY key) FROM scans), '[]'::jsonb),
    'windows', COALESCE((SELECT jsonb_object_agg(status, n) FROM counts), '{}'::jsonb),
    'account_first_day', 'unknown',
    'earliest_nonempty_window_date', (
        SELECT min(value->>'date')
        FROM requested
        WHERE value->>'completed_at' IS NOT NULL
          AND COALESCE(value->>'source_status', value->>'status') IN ('normalized','archived','partial')
    ),
    'plans', COALESCE((
        SELECT jsonb_agg(value - 'account' ORDER BY key) FROM app_state
        WHERE starts_with(key, 'syncplan:daily:')
    ), '[]'::jsonb),
    'semantics', 'requested daily windows, not percentage of all account history'
)::text
