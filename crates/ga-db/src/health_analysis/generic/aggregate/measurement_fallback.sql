WITH bounds AS (SELECT 'epoch'::timestamptz+$2::interval AS start,
    'epoch'::timestamptz+$3::interval AS finish,'epoch'::timestamptz+$4::interval AS cutoff), historical AS MATERIALIZED (
    SELECT history.* FROM measurement_history AS history CROSS JOIN bounds
    WHERE history.metric_definition_version_id=$1 AND history.quality='observed'
      AND $5 IN ('all','measurement') AND ($6::text IS NULL OR history.source=$6)
      AND CASE WHEN $7 THEN history.ts<bounds.start ELSE history.ts>=bounds.start AND history.ts<bounds.finish END
      AND history.ts<=bounds.cutoff AND history.known_at<=bounds.cutoff AND history.superseded_at>bounds.cutoff
    ORDER BY CASE WHEN $7 THEN history.ts END DESC,history.known_at DESC,history.id DESC
    LIMIT CASE WHEN $7 THEN 1 ELSE 10001 END
), ranked_history AS (
    SELECT historical.*,row_number() OVER(PARTITION BY ts,metric,source
        ORDER BY known_at DESC,id DESC) AS rank FROM historical
), current_values AS MATERIALIZED (
    SELECT current.*,CASE WHEN $7 THEN COALESCE(payload.fetched_at,current.ts)
        ELSE COALESCE(history.activation,payload.fetched_at,current.ingested_at) END AS known
    FROM measurements AS current CROSS JOIN bounds
    LEFT JOIN source_payloads AS payload ON payload.id=current.source_ref
    LEFT JOIN LATERAL (SELECT max(superseded_at) AS activation FROM measurement_history
        WHERE ts=current.ts AND metric=current.metric AND source=current.source) AS history ON true
    WHERE current.metric_definition_version_id=$1 AND current.quality='observed'
      AND $5 IN ('all','measurement') AND ($6::text IS NULL OR current.source=$6)
      AND CASE WHEN $7 THEN current.ts<bounds.start ELSE current.ts>=bounds.start AND current.ts<bounds.finish END
      AND current.ts<=bounds.cutoff
      AND COALESCE(history.activation,payload.fetched_at,current.ingested_at)<=bounds.cutoff
    ORDER BY CASE WHEN $7 THEN current.ts END DESC,CASE WHEN $7 THEN payload.fetched_at END DESC,
        CASE WHEN NOT $7 THEN current.ts END,CASE WHEN NOT $7 THEN current.metric END,
        CASE WHEN NOT $7 THEN current.source END
    LIMIT CASE WHEN $7 THEN 1 ELSE 10001 END
), combined AS (
    SELECT true AS historical,'measurement-history:'||id::text AS id,ts,metric,source,value,source_ref,known_at AS known
    FROM ranked_history WHERE rank=1
    UNION ALL
    SELECT false AS historical,'measurement:'||metric||':'||source||':'||
        overlay(to_jsonb(date_trunc('second',ts))#>>'{}' placing
            CASE WHEN extract(microseconds FROM ts)::bigint%1000000=0 THEN ''
                ELSE '.'||to_char(ts,'US') END from 20 for 0),ts,metric,source,value,source_ref,known
    FROM current_values
)
SELECT historical,id,(extract(epoch FROM ts)*1000000)::bigint AS observed,metric,source,value,source_ref,
    (extract(epoch FROM known)*1000000)::bigint AS ingested,
    overlay(to_jsonb(date_trunc('second',known))#>>'{}' placing
        CASE WHEN extract(microseconds FROM known)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(known,'US') END from 20 for 0) AS ingested_display
FROM combined CROSS JOIN bounds
WHERE $7 OR NOT EXISTS(
    SELECT FROM measurement_revisions AS revision
    WHERE revision.ts=combined.ts AND revision.metric=combined.metric
      AND revision.source=combined.source AND revision.metric_definition_version_id=$1
      AND revision.ingested_at<=bounds.cutoff
)
ORDER BY historical DESC
