WITH scoped AS NOT MATERIALIZED (
    SELECT job.id,raw.endpoint
    FROM jobs AS job
    JOIN source_payloads AS raw ON raw.id = CASE
        WHEN pg_input_is_valid(job.payload->>'raw_ref','uuid')
        THEN (job.payload->>'raw_ref')::uuid END
    WHERE job.kind='raw_replay' AND job.payload->>'target_version'=$1::int::text
      AND raw.source='garmin_connect' AND raw.endpoint=ANY($6::text[])
), exhausted AS (
    SELECT job.id FROM jobs AS job JOIN scoped USING (id)
    WHERE job.attempts >= $2
      AND (job.status='pending' OR (job.status='running' AND job.lease_until<$3))
    ORDER BY job.run_at,job.id
    FOR UPDATE OF job SKIP LOCKED LIMIT 100
), retired AS (
    UPDATE jobs SET status='failed',last_error='RetryLimitExceeded',
        completed_at=$3,lease_until=NULL,lease_token=NULL
    FROM exhausted WHERE jobs.id=exhausted.id
), candidate AS (
    SELECT job.id,scoped.endpoint FROM jobs AS job JOIN scoped USING (id)
    WHERE job.attempts<$2
      AND ((job.status='pending' AND job.run_at<=$3)
           OR (job.status='running' AND job.lease_until<$3))
    ORDER BY (job.payload->'backfill' IS NOT DISTINCT FROM 'true'::jsonb),job.run_at,job.id
    FOR UPDATE OF job SKIP LOCKED LIMIT 1
)
UPDATE jobs SET status='running',attempts=attempts+1,lease_token=$4,
                lease_until=$3+$5*interval '1 second'
FROM candidate WHERE jobs.id=candidate.id
RETURNING jobs.id,jobs.attempts,jobs.payload::text,candidate.endpoint
