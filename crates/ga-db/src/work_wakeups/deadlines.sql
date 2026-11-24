-- Already-due rows may be blocked by another worker or an ordering fence.
-- They are checked on state changes and recovery, never scheduled at sleep(0).
-- The recovery scan bounds the horizon, so distant historical schedules do not
-- enlarge this query and time passing is still an independent wakeup source.
WITH instant AS MATERIALIZED (SELECT clock_timestamp() AS at),
job_deadlines AS (
    SELECT public.ga_work_topic(kind) AS topic, run_at AS due
    FROM public.jobs, instant
    WHERE status='pending' AND run_at>instant.at
      AND run_at<=instant.at+interval '30 seconds'
    UNION ALL
    SELECT public.ga_work_topic(kind), lease_until
    FROM public.jobs, instant
    WHERE status='running' AND lease_until>instant.at
      AND lease_until<=instant.at+interval '30 seconds'
), outbox_deadlines AS (
    SELECT next_attempt_at AS due FROM public.outbox_messages, instant
    WHERE state='queued' AND next_attempt_at>instant.at
      AND next_attempt_at<=instant.at+interval '30 seconds'
    UNION ALL
    SELECT lease_until FROM public.outbox_messages, instant
    WHERE state='sending' AND lease_until>instant.at
      AND lease_until<=instant.at+interval '30 seconds'
), deadlines AS (
    SELECT topic,due FROM job_deadlines WHERE topic IS NOT NULL
    UNION ALL
    SELECT topics.topic, outbox_deadlines.due
    FROM outbox_deadlines CROSS JOIN
      (VALUES ('telegram_outbox'), ('extensions')) AS topics(topic)
)
SELECT topic,
    greatest(0,ceil(extract(epoch FROM (min(due)-clock_timestamp()))*1000))::bigint
FROM deadlines GROUP BY topic
