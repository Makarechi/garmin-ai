SELECT night.start, night."end", jsonb_build_array(
    raw.payload->'dailySleepDTO'->'calendarDate',
    raw.payload->'dailySleepDTO'->'sleepStartTimestampGMT',
    raw.payload->'dailySleepDTO'->'sleepEndTimestampGMT'
)::text
FROM source_payloads AS raw
JOIN timeline_intervals AS night ON night.id = $1
JOIN app_state AS watermark ON watermark.key = concat('ingest:garmin_connect:sleep:', $2::text)
LEFT JOIN app_state AS metadata ON metadata.key = concat('ingest-meta:', raw.id::text)
WHERE raw.id = $3 AND night.evidence->>'source_ref' = $4
  AND raw.source = 'garmin_connect' AND raw.endpoint = 'sleep' AND raw.source_key = $2
  AND raw.parser_version = $5 AND raw.status = 'normalized'
  AND night.source = 'garmin_connect' AND night.label = 'sleep' AND night.confirmed
  AND watermark.value->>'source_ref' = raw.id::text
  AND watermark.value->'hash' = to_jsonb(raw.payload_hash)
  AND watermark.value->>'status' IN ('normalized', 'empty')
  AND (watermark.value->'latest_attempt' IS NULL
       OR jsonb_typeof(watermark.value->'latest_attempt') = 'object')
  AND watermark.value->'latest_attempt'->>'status' IS DISTINCT FROM 'error'
  AND watermark.value->'latest_attempt'->>'status' IS DISTINCT FROM 'pending'
  AND (metadata.key IS NULL OR (
      jsonb_typeof(metadata.value) = 'object'
      AND metadata.value->'failed_parser_version' IS DISTINCT FROM to_jsonb($5)
  ))
  AND jsonb_typeof(raw.payload->'dailySleepDTO') = 'object'
