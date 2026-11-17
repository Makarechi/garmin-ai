WITH policy(metric, endpoint, max_age) AS (
    VALUES ('hrv_nightly_avg', 'hrv', 1),
           ('sleep_score', 'sleep', 1),
           ('training_readiness_score', 'readiness', 0)
)
SELECT policy.metric, policy.endpoint, policy.max_age, newest.day, newest.source_ref
FROM policy
LEFT JOIN LATERAL (
    SELECT health_days.day, health_days.sources->>('field:' || policy.metric) AS source_ref
    FROM health_days
    WHERE health_days.day <= $1
      AND CASE policy.metric
            WHEN 'hrv_nightly_avg' THEN health_days.hrv_nightly_avg IS NOT NULL
            WHEN 'sleep_score' THEN health_days.sleep_score IS NOT NULL
            WHEN 'training_readiness_score' THEN health_days.training_readiness_score IS NOT NULL
          END
    ORDER BY health_days.day DESC LIMIT 1
) AS newest ON TRUE
ORDER BY policy.metric
