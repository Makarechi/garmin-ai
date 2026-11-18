WITH baseline AS (
    SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS threshold
    FROM measurements
    WHERE metric='heart_rate_bpm' AND quality='observed' AND source='garmin_connect'
      AND ts >= $1::timestamptz - interval '336 hours' AND ts < $1 - interval '24 hours'
    HAVING count(*) >= 200 AND count(DISTINCT (ts AT TIME ZONE $4)::date) >= 7
),
stress AS (
    SELECT ts,value,lag(ts) OVER ordered AS previous_ts,lag(value) OVER ordered AS previous_value
    FROM measurements
    WHERE metric='stress_score' AND quality='observed' AND source='garmin_connect'
      AND ts >= $2 AND ts < $3
    WINDOW ordered AS (ORDER BY ts)
),
segmented AS (
    SELECT ts,value,sum(CASE
        WHEN previous_value IS NULL OR previous_value < 85 OR ts - previous_ts > interval '5 minutes'
        THEN 1 ELSE 0 END) OVER (ORDER BY ts) AS segment
    FROM stress
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
  AND EXISTS (
      SELECT FROM segmented WHERE value>=85 GROUP BY segment
      HAVING min(ts)=$2 AND max(ts)+interval '2 minutes'=$3
         AND max(ts)-min(ts)>=interval '20 minutes'
  )
