WITH ranked AS (
    SELECT id,row_number() OVER (
        PARTITION BY split_part(dedup_key,':',1),payload->>'endpoint',payload->>'key'
        ORDER BY run_at,id
    ) AS position
    FROM jobs
    WHERE kind='garmin_endpoint' AND status='pending'
      AND split_part(dedup_key,':',1) IN ('frequent','morning','reconcile')
)
UPDATE jobs SET status='cancelled',last_error='SupersededByQueuedFetch',completed_at=$1
FROM ranked WHERE jobs.id=ranked.id AND ranked.position>1 AND jobs.status='pending'
