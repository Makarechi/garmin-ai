WITH cutoff AS (SELECT 'epoch'::timestamptz+$4::interval AS at), observations AS MATERIALIZED (
    SELECT observation.*,event.updated_at AS event_updated_at,
        prior.id AS audit_id,prior.after AS audit_state,
        CASE WHEN event.updated_at<=cutoff.at THEN jsonb_build_object(
            'source',event.source,'status',event.status,'validation_status',event.validation_status,
            'assertion_kind',event.assertion_kind,'topology',event.topology) END AS event_state
    FROM metric_observations AS observation CROSS JOIN cutoff
    LEFT JOIN events AS event ON event.id=observation.source_entry_id
    LEFT JOIN event_definition_versions AS event_version ON event_version.id=event.definition_version_id
    LEFT JOIN LATERAL (SELECT id,after FROM audit_log
        WHERE event_id=event.id AND created_at<=cutoff.at ORDER BY created_at DESC,id DESC LIMIT 1) AS prior ON true
    WHERE observation.metric_definition_version_id=$1
      AND observation.observed_at>=$2 AND observation.observed_at<$3
      AND observation.ingested_at<=cutoff.at AND observation.quality='observed'
      AND (observation.valid OR observation.invalidated_at>cutoff.at)
      AND ($6='all' OR ($6='event' AND observation.source_entry_id IS NOT NULL)
          OR ($6='observation' AND observation.source_entry_id IS NULL
              AND observation.account IS NOT DISTINCT FROM $7::text
              AND observation.device IS NOT DISTINCT FROM $8::text))
      AND (observation.source_entry_id IS NULL OR (
          (event.definition_version_id IS NULL OR event_version.allowed_operations @> '["query"]'::jsonb)
          AND COALESCE(prior.after->>'status',CASE WHEN event.updated_at<=cutoff.at THEN event.status END)='confirmed'
          AND COALESCE((prior.after->>'deleted')::boolean,
              CASE WHEN event.updated_at<=cutoff.at THEN event.deleted END) IS FALSE))
    ORDER BY observation.observed_at,observation.id LIMIT $5::bigint+1
), identities AS (
    SELECT DISTINCT ts,metric,source FROM measurement_revisions CROSS JOIN cutoff
    WHERE metric_definition_version_id=$1 AND ts>=$2 AND ts<$3 AND ingested_at<=cutoff.at
      AND $6 IN ('all','measurement') AND ($9::text IS NULL OR source=$9)
), ranked AS (
    SELECT revision.*,row_number() OVER (PARTITION BY revision.ts,revision.metric,revision.source
        ORDER BY revision.ingested_at DESC,revision.deleted,revision.id DESC) AS rank
    FROM measurement_revisions AS revision JOIN identities USING(ts,metric,source) CROSS JOIN cutoff
    WHERE revision.ingested_at<=cutoff.at
), revisions AS (
    SELECT ts,metric,source,value,source_ref,quality FROM ranked
    WHERE rank=1 AND NOT deleted AND quality='observed' AND metric_definition_version_id=$1
    ORDER BY ts,metric,source LIMIT $5::bigint+1
), legacy AS (
    SELECT current.ts,current.metric,current.source,current.value,current.source_ref,current.quality
    FROM measurements AS current CROSS JOIN cutoff
    WHERE current.metric_definition_version_id=$1 AND current.ts>=$2 AND current.ts<$3
      AND current.ingested_at<=cutoff.at AND current.quality='observed'
      AND $6 IN ('all','measurement') AND ($9::text IS NULL OR current.source=$9)
      AND NOT EXISTS(SELECT FROM measurement_revisions AS history
          WHERE history.ts=current.ts AND history.metric=current.metric AND history.source=current.source)
    ORDER BY current.ts,current.metric,current.source LIMIT $5::bigint+1
), measurements AS MATERIALIZED (
    SELECT * FROM (SELECT * FROM revisions UNION ALL SELECT * FROM legacy) AS combined
    ORDER BY ts,metric COLLATE "C",source COLLATE "C" LIMIT $5::bigint+1
), observation_rows AS (
    SELECT observation.observed_at,observation.id::text AS id,
        json_build_object(
            'id',observation.id,'observed_at',clock.display,
            'value',COALESCE(to_json(observation.value),to_json(observation.value_text),to_json(observation.value_boolean)),
            'source_ref',observation.source_ref,'source',snapshot.value->'source',
            'metric_source',CASE WHEN observation.source_entry_id IS NOT NULL THEN 'event'
                ELSE 'observation:'||pair.value END,
            'projection_version',observation.projection_version,'quality',observation.quality,
            'owner_confirmation',snapshot.value->'status','validation_status',snapshot.value->'validation_status',
            'assertion_kind',snapshot.value->'assertion_kind','topology',snapshot.value->'topology'
        ) AS value
    FROM observations AS observation
    CROSS JOIN LATERAL (SELECT CASE WHEN observation.audit_id IS NOT NULL
        THEN observation.audit_state ELSE observation.event_state END AS value) AS snapshot
    CROSS JOIN LATERAL (SELECT '['||COALESCE(to_json(observation.account)::text,'null')||','||
        COALESCE(to_json(observation.device)::text,'null')||']' AS value) AS raw_pair
    CROSS JOIN LATERAL (SELECT string_agg(CASE
        WHEN ascii(character)<127 THEN character
        WHEN ascii(character)<=65535 THEN chr(92)||'u'||lpad(to_hex(ascii(character)),4,'0')
        ELSE chr(92)||'u'||to_hex(55296+((ascii(character)-65536)/1024))||
            chr(92)||'u'||to_hex(56320+((ascii(character)-65536)%1024)) END,'' ORDER BY position) AS value
        FROM regexp_split_to_table(raw_pair.value,'') WITH ORDINALITY AS chars(character,position)) AS pair
    CROSS JOIN LATERAL (SELECT overlay(to_jsonb(date_trunc('second',observation.observed_at))#>>'{}' placing
        CASE WHEN extract(microseconds FROM observation.observed_at)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(observation.observed_at,'US') END from 20 for 0) AS display) AS clock
), measurement_rows AS (
    SELECT measurement.ts AS observed_at,
        'measurement:'||measurement.metric||':'||measurement.source||':'||clock.display AS id,
        json_build_object(
            'id','measurement:'||measurement.metric||':'||measurement.source||':'||clock.display,
            'observed_at',clock.display,'value',measurement.value,'source_ref',measurement.source_ref,
            'source',NULL,'metric_source','measurement:'||measurement.source,
            'projection_version',NULL,'quality',measurement.quality,'owner_confirmation',NULL,
            'validation_status',NULL,'assertion_kind',NULL,'topology',NULL
        ) AS value
    FROM measurements AS measurement
    CROSS JOIN LATERAL (SELECT overlay(to_jsonb(date_trunc('second',measurement.ts))#>>'{}' placing
        CASE WHEN extract(microseconds FROM measurement.ts)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(measurement.ts,'US') END from 20 for 0) AS display) AS clock
), rows AS MATERIALIZED (
    SELECT * FROM observation_rows UNION ALL SELECT * FROM measurement_rows
), total AS (
    SELECT count(*) AS count,COALESCE((SELECT bool_or(audit_id IS NOT NULL
        AND (audit_state IS NULL OR jsonb_typeof(audit_state)<>'object')) FROM observations),false)
        AS invalid_snapshot FROM rows
), document AS MATERIALIZED (
    SELECT convert_to(json_build_object(
        'spec_hash',$10::text,'metric',$11::text,'metric_version',contract.version,
        'unit',contract.unit,'scale_id',contract.scale_id,'scale_version',contract.scale_version,
        'category_domain',contract.category_domain,'knowledge_cutoff',$12::text,
        'rows',COALESCE((SELECT json_agg(value ORDER BY observed_at,id COLLATE "C") FROM rows
            WHERE (SELECT count<=$5 AND NOT invalid_snapshot FROM total)),'[]'::json)
    )::text,'UTF8') AS bytes FROM metric_definition_versions AS contract WHERE id=$1
)
SELECT total.invalid_snapshot,total.count,substring(bytes FROM byte_offset FOR 65536)
FROM document CROSS JOIN total
CROSS JOIN LATERAL generate_series(1,octet_length(bytes),65536) AS parts(byte_offset)
ORDER BY byte_offset
