SELECT dates.day, raw.id, jsonb_strip_nulls(jsonb_build_object(
    'duration_minutes', CASE WHEN h.sources->>'field:sleep_seconds'=raw.id::text THEN round(h.sleep_seconds/60) END,
    'deep_minutes', CASE WHEN h.sources->>'field:deep_seconds'=raw.id::text THEN round(h.deep_seconds/60) END,
    'light_minutes', CASE WHEN h.sources->>'field:light_seconds'=raw.id::text THEN round(h.light_seconds/60) END,
    'rem_minutes', CASE WHEN h.sources->>'field:rem_seconds'=raw.id::text THEN round(h.rem_seconds/60) END,
    'awake_minutes', CASE WHEN h.sources->>'field:awake_seconds'=raw.id::text THEN round(h.awake_seconds/60) END,
    'score', CASE WHEN h.sources->>'field:sleep_score'=raw.id::text THEN h.sleep_score END
))::text,
night.start, night."end", jsonb_build_array(
    raw.dto->'calendarDate',
    raw.dto->'sleepStartTimestampGMT',
    raw.dto->'sleepEndTimestampGMT'
)::text
FROM (
    SELECT $1::date + step AS day
    FROM generate_series(0, $2::date - $1::date) AS steps(step)
    LIMIT 59
) AS dates
LEFT JOIN LATERAL (
    SELECT hr.sleep_seconds,hr.deep_seconds,hr.light_seconds,hr.rem_seconds,
           hr.awake_seconds,hr.sleep_score,hr.sources
    FROM health_days AS hr WHERE hr.day=dates.day LIMIT 1
) AS h ON true
LEFT JOIN LATERAL (
    SELECT state.value FROM app_state AS state
    WHERE state.key=concat('ingest:garmin_connect:sleep:',dates.day) LIMIT 1
) AS w ON true
LEFT JOIN LATERAL (
    SELECT r.id, CASE WHEN dates.day >= $4::date THEN r.payload->'dailySleepDTO' END AS dto
    FROM source_payloads AS r
    WHERE r.id=CASE
        WHEN pg_input_is_valid(w.value->>'source_ref','uuid') THEN (w.value->>'source_ref')::uuid
    END
      AND r.source='garmin_connect' AND r.endpoint='sleep' AND r.source_key=dates.day::text
      AND r.status='normalized' AND r.parser_version=$3
      AND w.value->'hash'=to_jsonb(r.payload_hash)
      AND w.value->>'status' IN ('normalized','empty')
      AND (w.value->'latest_attempt' IS NULL OR jsonb_typeof(w.value->'latest_attempt')='object')
      AND w.value->'latest_attempt'->>'status' IS DISTINCT FROM 'error'
      AND w.value->'latest_attempt'->>'status' IS DISTINCT FROM 'pending'
      AND CASE WHEN dates.day >= $4::date THEN jsonb_typeof(r.payload->'dailySleepDTO')='object' ELSE true END
      AND NOT EXISTS (
        SELECT FROM app_state AS m WHERE m.key=concat('ingest-meta:',r.id)
          AND (jsonb_typeof(m.value) IS DISTINCT FROM 'object' OR m.value->'failed_parser_version'=to_jsonb($3))
      )
    LIMIT 1
) AS raw ON true
LEFT JOIN LATERAL (
    SELECT t.start,t."end"
    FROM timeline_intervals AS t WHERE t.id=concat('sleep:',dates.day)
      AND t.source='garmin_connect' AND t.label='sleep' AND t.confirmed
      AND t.evidence->>'source_ref'=raw.id::text
    LIMIT 1
) AS night ON true
ORDER BY dates.day
