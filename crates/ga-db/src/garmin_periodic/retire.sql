WITH pending AS (
    SELECT id FROM jobs
    WHERE kind IN ('garmin_endpoint','garmin_activities','garmin_fit') AND status='pending'
    ORDER BY id FOR UPDATE SKIP LOCKED
), retired AS (
    UPDATE jobs SET status='failed',last_error='IntegrationDisabled',completed_at=$1,
                    lease_until=NULL,lease_token=NULL
    FROM pending WHERE jobs.id=pending.id RETURNING jobs.payload->>'sync_window' AS window_key
), windows AS (
    UPDATE app_state SET value=value || jsonb_build_object('status','disabled','disabled_at',$2::text)
    WHERE value->>'status'='pending' AND key IN (SELECT window_key FROM retired)
)
SELECT count(*) FROM retired
