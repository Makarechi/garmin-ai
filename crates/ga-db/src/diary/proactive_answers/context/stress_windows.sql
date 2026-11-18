WITH stress AS (
    SELECT ts,value,lag(ts) OVER ordered AS previous_ts,lag(value) OVER ordered AS previous_value
    FROM measurements
    WHERE metric='stress_score' AND quality='observed' AND source='garmin_connect'
      AND ts >= $1 AND ts < $2
    WINDOW ordered AS (ORDER BY ts)
),
segmented AS (
    SELECT ts,value,sum(CASE
        WHEN previous_value IS NULL OR previous_value < 85 OR ts - previous_ts > interval '5 minutes'
        THEN 1 ELSE 0 END) OVER (ORDER BY ts) AS segment
    FROM stress
)
SELECT min(ts),max(ts)+interval '2 minutes'
FROM segmented
WHERE value>=85
GROUP BY segment
HAVING max(ts)-min(ts)>=interval '20 minutes'
ORDER BY min(ts)
