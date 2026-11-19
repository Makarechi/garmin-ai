WITH bounds AS (SELECT 'epoch'::timestamptz+$2::interval AS start,
    'epoch'::timestamptz+$3::interval AS finish,'epoch'::timestamptz+$4::interval AS cutoff), identities AS (
    SELECT DISTINCT ts,metric,source FROM measurement_revisions CROSS JOIN bounds
    WHERE metric_definition_version_id=$1 AND (bounds.start IS NULL OR ts>=bounds.start)
      AND ts<bounds.finish AND ingested_at<=bounds.cutoff AND ($5::text IS NULL OR source=$5)
), ranked AS (
    SELECT revision.*,row_number() OVER(PARTITION BY revision.ts,revision.metric,revision.source
        ORDER BY revision.ingested_at DESC,revision.deleted,revision.id DESC) AS rank
    FROM measurement_revisions AS revision JOIN identities USING(ts,metric,source) CROSS JOIN bounds
    WHERE revision.ingested_at<=bounds.cutoff
), revisions AS (
    SELECT id,ts,metric,source,value,unit,metric_definition_version_id,source_ref,quality,
        '{}'::jsonb AS details,ingested_at,deleted FROM ranked WHERE rank=1
      AND ($8 OR (NOT deleted AND quality='observed' AND metric_definition_version_id=$1))
    ORDER BY CASE WHEN $7 THEN ts END DESC,CASE WHEN NOT $7 THEN ts END,metric,source LIMIT $6::bigint
), legacy AS (
    SELECT NULL::uuid AS id,ts,metric,source,value,unit,metric_definition_version_id,
        source_ref,quality,details,ingested_at,false AS deleted
    FROM measurements AS current CROSS JOIN bounds
    WHERE metric_definition_version_id=$1 AND (bounds.start IS NULL OR ts>=bounds.start)
      AND ts<bounds.finish AND ingested_at<=bounds.cutoff AND ($5::text IS NULL OR source=$5)
      AND ($8 OR quality='observed')
      AND NOT EXISTS(SELECT FROM measurement_revisions AS revision
          WHERE revision.ts=current.ts AND revision.metric=current.metric AND revision.source=current.source)
    ORDER BY CASE WHEN $7 THEN ts END DESC,CASE WHEN NOT $7 THEN ts END,metric,source LIMIT $6::bigint
), combined AS (
    SELECT * FROM revisions UNION ALL SELECT * FROM legacy
)
SELECT id,(extract(epoch FROM ts)*1000000)::bigint AS observed,metric,source,value,unit,
    metric_definition_version_id,source_ref,quality,
    (CASE WHEN id IS NULL AND $9 THEN details ELSE '{}'::jsonb END)::text AS details,
    (extract(epoch FROM ingested_at)*1000000)::bigint AS ingested,
    overlay(to_jsonb(date_trunc('second',ts))#>>'{}' placing
        CASE WHEN extract(microseconds FROM ts)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(ts,'US') END from 20 for 0) AS observed_display,
    overlay(to_jsonb(date_trunc('second',ingested_at))#>>'{}' placing
        CASE WHEN extract(microseconds FROM ingested_at)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(ingested_at,'US') END from 20 for 0) AS ingested_display,
    deleted
FROM combined
ORDER BY CASE WHEN $7 THEN ts END DESC,CASE WHEN NOT $7 THEN ts END,
    CASE WHEN $7 THEN metric END COLLATE "C" DESC,CASE WHEN $7 THEN source END COLLATE "C" DESC,
    metric COLLATE "C",source COLLATE "C" LIMIT $6::bigint
