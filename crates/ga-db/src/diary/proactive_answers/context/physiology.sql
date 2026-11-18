WITH baseline AS (
    SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS threshold
    FROM measurements
    WHERE metric='heart_rate_bpm' AND quality='observed' AND source='garmin_connect'
      AND ts >= $1::timestamptz - interval '336 hours' AND ts < $1 - interval '24 hours'
    HAVING count(*) >= 200 AND count(DISTINCT (ts AT TIME ZONE $4)::date) >= 7
),
hr AS (
    SELECT ts,value,lead(ts) OVER (ORDER BY ts) AS next_ts
    FROM measurements
    WHERE metric='heart_rate_bpm' AND quality='observed' AND source='garmin_connect'
      AND ts >= $2 AND ts < $3
),
observations AS (
    SELECT count(*) AS samples,avg(value) AS mean,
           coalesce(sum(EXTRACT(EPOCH FROM next_ts-ts))
             FILTER (WHERE next_ts-ts <= interval '5 minutes'),0)::double precision
              / EXTRACT(EPOCH FROM $3::timestamptz-$2::timestamptz) AS coverage
    FROM hr
)
SELECT threshold,samples,coverage
FROM baseline CROSS JOIN observations
WHERE samples>=5 AND mean>=threshold AND coverage>=0.8
