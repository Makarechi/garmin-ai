WITH RECURSIVE eligible AS MATERIALIZED (
    SELECT id,source_ref,metric,value,observed_at,fetched_at,ingested_at,sequence,
           effective_start,timezone,source_calendar_date
    FROM metric_observations
    WHERE metric=ANY($1::text[]) AND observed_at>=$3 AND observed_at<$4
      AND ingested_at<=$5 AND quality='observed' AND feature_version=$6
    -- The bounded set is complete when the budget permits a result.
    -- Version preference is resolved by the later distinct-time ordering.
    LIMIT 100001
), budget AS MATERIALIZED (
    SELECT count(*)<=100000 AS within_limit FROM eligible
), distinct_times AS (
    SELECT DISTINCT ON(metric,observed_at::timestamp) eligible.*
    FROM eligible CROSS JOIN budget WHERE within_limit
    ORDER BY metric,observed_at::timestamp,observed_at DESC,fetched_at DESC,ingested_at DESC,sequence DESC
), preferred AS MATERIALIZED (
    SELECT *,row_number() OVER(PARTITION BY metric ORDER BY observed_at::timestamp) AS position
    FROM distinct_times
), indexed AS MATERIALIZED (
    SELECT requested.metric,COALESCE(array_agg(preferred.observed_at ORDER BY preferred.position)
        FILTER(WHERE preferred.position IS NOT NULL),'{}'::timestamptz[]) AS times
    FROM unnest($1::text[]) AS requested(metric)
    LEFT JOIN preferred ON preferred.metric=requested.metric
    CROSS JOIN budget WHERE within_limit GROUP BY requested.metric
), search(metric,clock,low,high) AS (
    SELECT metric,clock,0,cardinality(times) FROM indexed
    CROSS JOIN unnest($2::timestamptz[]) AS events(clock)
    UNION ALL
    SELECT search.metric,search.clock,
        CASE WHEN CASE WHEN $7 THEN indexed.times[(low+high)/2+1]::timestamp<search.clock::timestamp ELSE indexed.times[(low+high)/2+1]<search.clock END THEN (low+high)/2+1 ELSE low END,
        CASE WHEN CASE WHEN $7 THEN indexed.times[(low+high)/2+1]::timestamp<search.clock::timestamp ELSE indexed.times[(low+high)/2+1]<search.clock END THEN high ELSE (low+high)/2 END
    FROM search JOIN indexed USING(metric) WHERE low<high
), selected AS (
    SELECT search.clock,search.metric,preferred.value,preferred.id,preferred.source_ref,
        preferred.observed_at,preferred.ingested_at,preferred.effective_start,
        preferred.timezone,preferred.source_calendar_date FROM search
    LEFT JOIN preferred ON preferred.metric=search.metric AND preferred.position=search.low
        AND CASE WHEN $7 THEN preferred.observed_at::timestamp>=search.clock::timestamp-interval '24 hours'
            ELSE preferred.observed_at>=search.clock-interval '24 hours' END
    WHERE low=high
)
SELECT selected.clock,selected.metric,selected.value,selected.id,selected.source_ref,
       displays.observed_at,displays.ingested_at,displays.effective_start,
       selected.timezone,selected.source_calendar_date
FROM selected
CROSS JOIN LATERAL (
    SELECT overlay(to_jsonb(date_trunc('second',selected.observed_at))#>>'{}' placing
        CASE WHEN extract(microseconds FROM selected.observed_at)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(selected.observed_at,'US') END from 20 for 0) AS observed_at,
        overlay(to_jsonb(date_trunc('second',selected.ingested_at))#>>'{}' placing
        CASE WHEN extract(microseconds FROM selected.ingested_at)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(selected.ingested_at,'US') END from 20 for 0) AS ingested_at,
        overlay(to_jsonb(date_trunc('second',selected.effective_start))#>>'{}' placing
        CASE WHEN extract(microseconds FROM selected.effective_start)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(selected.effective_start,'US') END from 20 for 0) AS effective_start
) AS displays
UNION ALL
SELECT NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL FROM budget WHERE NOT within_limit
