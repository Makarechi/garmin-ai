WITH RECURSIVE bounds AS MATERIALIZED (
    SELECT $1::timestamptz AS first,$2::timestamptz AS last
), clock(at) AS (
    SELECT first FROM bounds
    UNION ALL
    SELECT least(last,at+make_interval(secs=>
        3600-extract(minute FROM at AT TIME ZONE $3)*60
            -extract(second FROM at AT TIME ZONE $3)))
    FROM clock,bounds WHERE at<last
), clock_intervals AS (
    SELECT at,lead(at) OVER (ORDER BY at) AS finish FROM clock
), schedule AS (
    SELECT extract(hour FROM at AT TIME ZONE $3)::integer AS hour,
        sum(extract(epoch FROM finish-at))::double precision AS seconds
    FROM clock_intervals WHERE finish IS NOT NULL GROUP BY hour
), samples AS MATERIALIZED (
    SELECT ts,value FROM measurements,bounds
    WHERE metric='stress_score' AND source='garmin_connect'
      AND quality='observed' AND unit='score' AND value BETWEEN 0 AND 100
      AND ts>=first-interval '5 minutes' AND ts<=last+interval '5 minutes'
    ORDER BY ts LIMIT 100001
), cap AS MATERIALIZED (
    SELECT count(*)>100000 AS exceeded FROM samples
), following AS (
    SELECT ts,value,lead(ts) OVER (ORDER BY ts) AS next_ts FROM samples
    WHERE NOT (SELECT exceeded FROM cap)
), segments AS (
    SELECT greatest(ts,first) AS at,least(next_ts,last) AS finish,value
    FROM following,bounds WHERE next_ts>ts AND next_ts-ts<=interval '5 minutes'
      AND greatest(ts,first)<least(next_ts,last)
), pieces(at,stop,finish,value) AS (
    SELECT at,least(finish,at+make_interval(secs=>
        3600-extract(minute FROM at AT TIME ZONE $3)*60
            -extract(second FROM at AT TIME ZONE $3))),finish,value FROM segments
    UNION ALL
    SELECT stop,least(finish,stop+make_interval(secs=>
        3600-extract(minute FROM stop AT TIME ZONE $3)*60
            -extract(second FROM stop AT TIME ZONE $3))),finish,value
    FROM pieces WHERE stop<finish
), observed AS (
    SELECT extract(hour FROM at AT TIME ZONE $3)::integer AS hour,
        sum(extract(epoch FROM stop-at)::double precision ORDER BY at) AS covered,
        sum(extract(epoch FROM stop-at)::double precision ORDER BY at)
            FILTER (WHERE value>=51) AS elevated,
        sum(extract(epoch FROM stop-at)::double precision*value ORDER BY at) AS weighted,
        count(DISTINCT (at AT TIME ZONE $3)::date) AS days
    FROM pieces GROUP BY hour
)
SELECT hours.hour,cap.exceeded AS over_limit,coalesce(schedule.seconds,0) AS scheduled,
    coalesce(observed.covered,0) AS covered,coalesce(observed.elevated,0) AS elevated,
    coalesce(observed.weighted,0) AS weighted,coalesce(observed.days,0) AS days
FROM generate_series(0,23) AS hours(hour) CROSS JOIN cap
LEFT JOIN schedule USING(hour) LEFT JOIN observed USING(hour) ORDER BY hour
