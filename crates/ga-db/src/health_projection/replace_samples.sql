WITH revisions AS MATERIALIZED (
    SELECT id FROM source_payloads WHERE source=$1 AND endpoint=$2 AND source_key=$3
), removed AS (
    DELETE FROM measurements AS current USING revisions
    WHERE current.source_ref=revisions.id AND current.metric=ANY($4::text[])
      AND current.ts >= $5 AND current.ts < $6 AND current.ingested_at <= $7
    RETURNING current.*
), tombstones AS (
    INSERT INTO measurement_revisions (
        id,ts,metric,source,local_date,value,unit,metric_definition_version_id,
        source_ref,quality,details,ingested_at,deleted
    )
    SELECT gen_random_uuid(),ts,metric,source,local_date,value,unit,metric_definition_version_id,
           source_ref,quality,details,$7,true
    FROM removed ON CONFLICT (ts,metric,source,source_ref,ingested_at,deleted) DO NOTHING
)
DELETE FROM app_state AS owner
USING revisions
WHERE starts_with(owner.key,'sample-owner:') AND owner.value->>'source_ref'=revisions.id::text
  AND owner.value->>'metric'=ANY($4::text[])
  AND (owner.value->>'ts')::timestamptz >= $5 AND (owner.value->>'ts')::timestamptz < $6
  AND NOT EXISTS (
      SELECT 1 FROM measurements AS newer
      WHERE newer.ts=(owner.value->>'ts')::timestamptz AND newer.metric=owner.value->>'metric'
        AND newer.source=owner.value->>'source' AND newer.ingested_at > $7
  )
  AND NOT EXISTS (
      SELECT 1 FROM app_state AS metadata
      WHERE metadata.key=concat('ingest-meta:', revisions.id::text)
        AND (metadata.value->>'applied_at')::timestamptz > $7
  )
