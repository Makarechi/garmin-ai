WITH policy(metric, endpoint, gap, lag_limit, refresh_mode) AS (
    VALUES ('heart_rate_bpm', 'heart_rate', 300, 1800, 'frequent'),
           ('stress_score', 'stress', 300, 1800, 'frequent'),
           ('body_battery', 'stress', 900, 3600, 'frequent'),
           ('respiration_rpm', 'respiration', 300, 1800, 'daily'),
           ('spo2_pct', 'spo2', 3600, 7200, 'daily')
), bounds AS (
    SELECT $1::timestamptz AS at, $2::timestamptz AS midnight, $3::jsonb AS technical
)
SELECT policy.metric, policy.endpoint, policy.gap, policy.lag_limit, policy.refresh_mode,
       newest.ts, newest.source_ref, coverage.observed_start, coverage.observed_end,
       COALESCE(coverage.seconds, 0)::float8,
       COALESCE(coverage.recent_seconds, 0)::float8,
       EXISTS (
           SELECT 1 FROM measurements AS returned
           WHERE returned.source_ref = CASE
                WHEN COALESCE(bounds.technical->policy.endpoint->>'status', '') NOT IN ('error','fetch_error')
                THEN NULLIF(bounds.technical->policy.endpoint->>'source_ref', '')::uuid
           END
             AND returned.metric = policy.metric AND returned.source = 'garmin_connect'
             AND returned.quality = 'observed' AND returned.ts <= bounds.at
       ) AS fetch_has_samples
FROM policy CROSS JOIN bounds
LEFT JOIN LATERAL (
    SELECT measurements.ts, measurements.source_ref
    FROM measurements
    WHERE measurements.metric = policy.metric AND measurements.source = 'garmin_connect'
      AND measurements.quality = 'observed' AND measurements.ts <= bounds.at
    ORDER BY measurements.ts DESC LIMIT 1
) AS newest ON TRUE
CROSS JOIN LATERAL (
    SELECT min(ts) FILTER (WHERE ts >= bounds.midnight) AS observed_start,
           max(ts) FILTER (WHERE ts >= bounds.midnight) AS observed_end,
           sum(greatest(0, extract(epoch FROM (ts - greatest(prev, bounds.midnight)))))
               FILTER (WHERE ts - prev <= policy.gap * interval '1 second') AS seconds,
           sum(greatest(0, extract(epoch FROM
               (ts - greatest(prev, bounds.at - policy.lag_limit * interval '1 second')))))
               FILTER (WHERE ts - prev <= policy.gap * interval '1 second') AS recent_seconds
    FROM (
        SELECT measurements.ts, lag(measurements.ts) OVER (ORDER BY measurements.ts) AS prev
        FROM measurements
        WHERE measurements.metric = policy.metric AND measurements.source = 'garmin_connect'
          AND measurements.quality = 'observed'
          AND measurements.ts BETWEEN bounds.at - interval '48 hours' AND bounds.at
    ) AS paired
) AS coverage
ORDER BY policy.metric
