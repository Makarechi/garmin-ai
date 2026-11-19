WITH cutoff AS (SELECT 'epoch'::timestamptz+$2::interval AS at), known AS (
    SELECT event.id,event.kind,event.ingested_at,event.updated_at,
        CASE WHEN prior.id IS NOT NULL THEN prior.after ELSE later.before END AS snapshot,
        jsonb_build_object(
            'id',event.id,'definition_version_id',event.definition_version_id,
            'revision',event.revision,'start',
                overlay(to_jsonb(date_trunc('second',event.start))#>>'{}' placing
                    CASE WHEN extract(microseconds FROM event.start)::bigint%1000000=0
                        THEN '' ELSE '.'||to_char(event.start,'US') END from 20 for 0),
            'end',overlay(to_jsonb(date_trunc('second',event."end"))#>>'{}' placing
                    CASE WHEN extract(microseconds FROM event."end")::bigint%1000000=0
                        THEN '' ELSE '.'||to_char(event."end",'US') END from 20 for 0),
            'payload',event.payload,'status',event.status,'validation_status',event.validation_status,
            'assertion_kind',event.assertion_kind,'source',event.source,
            'topology',event.topology,'deleted',event.deleted,'kind',event.kind
        ) AS live
    FROM events AS event CROSS JOIN cutoff
    LEFT JOIN LATERAL (SELECT id,after FROM audit_log WHERE event_id=event.id AND created_at<=cutoff.at
        ORDER BY created_at DESC,id DESC LIMIT 1) AS prior ON true
    LEFT JOIN LATERAL (SELECT id,before FROM audit_log WHERE event_id=event.id AND created_at>cutoff.at
        ORDER BY created_at,id LIMIT 1) AS later ON true
    WHERE event.id=ANY($1::uuid[])
), reconstructed AS (
    SELECT id,kind,
        CASE WHEN snapshot IS NULL OR snapshot='null'::jsonb THEN
            CASE WHEN ingested_at<=cutoff.at AND updated_at<=cutoff.at THEN live END
            ELSE snapshot END AS snapshot,
        (snapshot IS NULL OR snapshot='null'::jsonb)
            AND ingested_at<=cutoff.at AND updated_at>cutoff.at AS unavailable
    FROM known CROSS JOIN cutoff
), flags AS (
    SELECT COALESCE(bool_or(unavailable),false) AS unavailable FROM reconstructed
), normalized AS (
    SELECT reconstructed.snapshot || jsonb_build_object('topology',
        COALESCE(NULLIF(snapshot->>'topology',''),CASE
            WHEN snapshot->>'end' IS NULL AND
                CASE WHEN snapshot ? 'kind' THEN snapshot->>'kind' ELSE kind END IN ('migraine','illness')
                THEN 'open_interval'
            WHEN snapshot->>'end' IS NULL OR (snapshot->>'end')::timestamptz=(snapshot->>'start')::timestamptz
                THEN 'point' ELSE 'bounded_interval' END)
    ) AS snapshot
    FROM reconstructed WHERE snapshot IS NOT NULL AND snapshot<>'null'::jsonb
        AND NOT COALESCE((snapshot->>'deleted')::boolean,false)
        AND NOT (SELECT unavailable FROM flags)
), eligible AS MATERIALIZED (
    SELECT snapshot FROM normalized JOIN event_definition_versions AS version
        ON version.id=NULLIF(snapshot->>'definition_version_id','')::uuid
    WHERE version.definition_id=$3 AND version.allowed_operations ? 'query'
      AND CASE WHEN $6='starts_within' THEN
        (snapshot->>'start')::timestamptz>=$4 AND (snapshot->>'start')::timestamptz<$5
      ELSE (snapshot->>'start')::timestamptz<$5 AND (
        (snapshot->>'end')::timestamptz>$4
        OR (snapshot->>'end' IS NULL AND snapshot->>'topology'='open_interval')
        OR ((snapshot->>'start')::timestamptz>=$4 AND snapshot->>'topology' IN ('point','flexible')
            AND (snapshot->>'end' IS NULL OR (snapshot->>'end')::timestamptz=(snapshot->>'start')::timestamptz))
      ) END
), total AS (SELECT count(*) AS count FROM eligible), source AS MATERIALIZED (
    SELECT convert_to(json_build_object(
        'spec_hash',$8::text,'definition_key',$9::text,'knowledge_cutoff',$10::text,
        'rows',COALESCE((SELECT json_agg(json_build_object(
            'id',snapshot->'id','definition_version_id',snapshot->'definition_version_id',
            'revision',snapshot->'revision','start',snapshot->'start','end',snapshot->'end',
            'payload',snapshot->'payload','status',snapshot->'status',
            'validation_status',snapshot->'validation_status','assertion_kind',snapshot->'assertion_kind',
            'source',snapshot->'source','topology',snapshot->'topology'
        ) ORDER BY (snapshot->>'start') COLLATE "C",(snapshot->>'id') COLLATE "C")
            FROM eligible WHERE (SELECT count FROM total)<=$7),'[]'::json)
    )::text,'UTF8') AS bytes
)
SELECT flags.unavailable,total.count,substring(bytes FROM byte_offset FOR 65536)
FROM source CROSS JOIN flags CROSS JOIN total
CROSS JOIN LATERAL generate_series(1,octet_length(bytes),65536) AS parts(byte_offset)
ORDER BY byte_offset
