WITH normalized AS (
    SELECT CASE jobs.kind
               WHEN 'telegram_native_inbound' THEN 'telegram_update'
               WHEN 'telegram_native_ack' THEN 'telegram_ack'
               ELSE CASE WHEN jobs.kind=ANY($1) THEN jobs.kind ELSE 'other' END
           END AS kind,
           CASE WHEN jobs.status=ANY($2) THEN jobs.status ELSE 'other' END AS status,
           CASE WHEN jobs.status='pending' AND run_at<=$3 THEN run_at END AS due
    FROM jobs
)
SELECT kind,status,count(*),count(due),min(due)
FROM normalized
GROUP BY kind,status
ORDER BY kind,status
