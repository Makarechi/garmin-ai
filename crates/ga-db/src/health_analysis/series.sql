WITH samples AS MATERIALIZED (
    SELECT ts,source,value,source_ref FROM measurements
    WHERE metric=$1 AND unit=$2 AND quality='observed'
      AND value >= $3 AND value < 'Infinity'::double precision
      AND ($4::double precision IS NULL OR value <= $4)
      AND ts >= $5-make_interval(secs=>$7) AND ts <= $6+make_interval(secs=>$7)
    ORDER BY source,ts LIMIT 100001
), cap AS MATERIALIZED (
    SELECT count(*) > 100000 AS exceeded FROM samples
), bounded AS NOT MATERIALIZED (
    SELECT * FROM samples WHERE NOT (SELECT exceeded FROM cap)
), following AS (
    SELECT *,lead(ts) OVER source_order AS next_ts,
        lead(source_ref) OVER source_order AS next_ref
    FROM bounded WINDOW source_order AS (PARTITION BY source ORDER BY ts)
), pieces AS MATERIALIZED (
    SELECT date_bin(make_interval(secs=>$8),ts,$9) AS bucket,source,ts AS piece_at,
        1 AS samples,value,0::double precision AS weighted,0::double precision AS covered,
        ARRAY[source_ref] AS refs
    FROM bounded WHERE ts >= $5 AND ts < $6
    UNION ALL
    SELECT bucket,source,ts AS piece_at,0,value,
        extract(epoch FROM least(next_ts,$6,bucket+make_interval(secs=>$8))
            -greatest(ts,$5,bucket))::double precision * value,
        extract(epoch FROM least(next_ts,$6,bucket+make_interval(secs=>$8))
            -greatest(ts,$5,bucket))::double precision,
        ARRAY[source_ref,next_ref]
    FROM following
    CROSS JOIN LATERAL generate_series(
        date_bin(make_interval(secs=>$8),greatest(ts,$5),$9),
        date_bin(make_interval(secs=>$8),least(next_ts,$6)-interval '1 microsecond',$9),
        make_interval(secs=>$8)
    ) AS bucket
    WHERE $10 AND next_ts > ts AND extract(epoch FROM next_ts-ts) <= $7
      AND greatest(ts,$5) < least(next_ts,$6)
), totals AS MATERIALIZED (
    SELECT bucket,source,sum(samples) AS samples,
        sum(value ORDER BY piece_at) FILTER (WHERE NOT $10) AS total,
        sum(weighted ORDER BY piece_at) AS weighted,
        sum(covered ORDER BY piece_at) AS covered,
        min(value) AS minimum,max(value) AS maximum
    FROM pieces GROUP BY bucket,source
), source_references AS MATERIALIZED (
    SELECT bucket,source,array_agg(DISTINCT reference ORDER BY reference)
        FILTER (WHERE reference IS NOT NULL) AS refs
    FROM pieces CROSS JOIN LATERAL unnest(refs) AS reference
    WHERE reference IS NOT NULL GROUP BY bucket,source
), covered AS (
    SELECT totals.*,source_references.refs,
        covered/extract(epoch FROM least($6,bucket+make_interval(secs=>$8))
            -greatest($5,bucket))::double precision AS ratio
    FROM totals LEFT JOIN source_references USING (bucket,source)
), encoded AS (
    SELECT bucket,source,jsonb_build_object(
        'source',source,'samples',samples,
        'value',CASE WHEN NOT $10 THEN total WHEN ratio>=0.8 THEN weighted/covered END,
        'sum',CASE WHEN NOT $10 THEN total END,
        'mean',CASE WHEN $10 AND ratio>=0.8 THEN weighted/covered END,
        'min',minimum,'max',maximum,'unit',$2::text,
        'aggregation',CASE WHEN $10 THEN 'time_weighted_mean' ELSE 'sum' END,
        'coverage_ratio',CASE WHEN $10 THEN ratio END,
        'covered_seconds',CASE WHEN $10 THEN covered END,
        'source_refs',coalesce(refs[1:100],ARRAY[]::uuid[]),
        'source_refs_truncated',coalesce(cardinality(refs)>100,false)
    )::text AS document FROM covered
), combined AS (
    SELECT false AS over_limit,bucket,source,document FROM encoded
    UNION ALL
    SELECT true,NULL,NULL,NULL WHERE (SELECT exceeded FROM cap)
)
SELECT over_limit,bucket,document FROM combined ORDER BY bucket,source COLLATE "C" LIMIT $11
