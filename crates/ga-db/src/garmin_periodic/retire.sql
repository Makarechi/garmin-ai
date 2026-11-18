WITH pending AS (
    SELECT id FROM jobs
    WHERE kind IN ('garmin_endpoint','garmin_activities','garmin_fit')
      AND (status='pending' OR (status='running' AND lease_until<$1))
    ORDER BY id FOR UPDATE SKIP LOCKED
), retired AS (
    UPDATE jobs SET status='failed',last_error='IntegrationDisabled',completed_at=$1,
                    lease_until=NULL,lease_token=NULL
    FROM pending WHERE jobs.id=pending.id
    RETURNING jobs.id, jobs.payload->>'sync_window' AS window_key,
              jobs.payload->>'scan_key' AS scan_key, jobs.payload->>'generation' AS generation
), windows AS (
    UPDATE app_state SET value=value || jsonb_build_object('status','disabled','disabled_at',$2::text)
    WHERE value->>'status'='pending' AND key IN (SELECT window_key FROM retired)
), scans AS (
    UPDATE app_state state
    SET value=(state.value - 'next_scan_at') || jsonb_build_object('status','disabled','disabled_at',$2::text)
    FROM retired WHERE state.key=retired.scan_key AND state.value->>'status'='running'
      AND state.value->>'generation'=retired.generation AND state.value->>'job_id'=retired.id::text
)
SELECT count(*) FROM retired
