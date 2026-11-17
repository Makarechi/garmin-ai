WITH source_page AS MATERIALIZED (
    SELECT id, source, endpoint, source_key, status, fetched_at, parser_version, archive_key, payload
    FROM source_payloads
    WHERE source = 'garmin_connect' AND parser_version != $1
      AND ($2::uuid IS NULL OR id > $2)
    ORDER BY id
    LIMIT $3
), streams AS (
    SELECT DISTINCT source, endpoint, source_key
    FROM source_page
), stream_failures AS MATERIALIZED (
    SELECT stream.source, stream.endpoint, stream.source_key, failed.id
    FROM streams AS stream
    LEFT JOIN app_state AS watermark ON watermark.key =
        CASE WHEN stream.endpoint = 'activity_fit' THEN concat('fit-version:', stream.source_key)
             ELSE concat('ingest:', stream.source, ':', stream.endpoint, ':', stream.source_key)
        END
    JOIN LATERAL (
        SELECT candidate.id FROM source_payloads AS candidate
        WHERE candidate.source = stream.source AND candidate.endpoint = stream.endpoint
          AND candidate.source_key = stream.source_key AND candidate.status = 'error'
          AND (
              candidate.id::text = watermark.value->'latest_attempt'->>'source_ref'
              OR candidate.fetched_at > (watermark.value->>'requested_at')::timestamptz
              OR (
                  watermark.value->'latest_attempt'->>'source_ref' IS NULL
                  AND watermark.value->>'source_ref' IS NULL
                  AND candidate.fetched_at = (watermark.value->>'requested_at')::timestamptz
                  AND NOT EXISTS (
                      SELECT 1 FROM activities AS activity
                      WHERE stream.endpoint = 'activity_fit' AND activity.id = stream.source_key
                        AND activity.details->>'fit_status' = 'normalized'
                        AND activity.details->>'parsed_fit_key' IS NOT NULL
                  )
              )
          )
          AND NOT EXISTS (
              SELECT 1 FROM app_state AS attempted
              WHERE attempted.key = concat('ingest-meta:', candidate.id::text)
                AND (attempted.value->>'failed_parser_version')::int = $1
          )
        ORDER BY candidate.fetched_at DESC, candidate.id DESC
        LIMIT 1
    ) AS failed ON true
), projection_owner AS MATERIALIZED (
    SELECT value->>'source_ref' AS reference FROM app_state
    WHERE starts_with(key, 'sample-owner:')
       OR starts_with(key, 'interval-owner:')
       OR starts_with(key, 'observation-owner:')
       OR starts_with(key, 'activity-parts-owner:')
    UNION ALL
    SELECT expanded.value
    FROM app_state AS state
    CROSS JOIN LATERAL jsonb_array_elements_text(
        jsonb_path_query_array(state.value->'owners', 'lax $.*[*] ? (@.type() == "string")')
    ) AS expanded(value)
    WHERE starts_with(state.key, 'activity-version:')
    UNION ALL
    SELECT source_ref::text FROM measurements
    WHERE source_ref IN (SELECT id FROM source_page)
    UNION ALL
    SELECT source_ref::text FROM metric_observations
    WHERE source_ref IN (SELECT id FROM source_page)
    UNION ALL
    SELECT evidence->>'source_ref' FROM timeline_intervals
    UNION ALL
    SELECT field.value
    FROM health_days AS day
    CROSS JOIN LATERAL jsonb_each_text(
        CASE WHEN jsonb_typeof(day.sources) = 'object' THEN day.sources ELSE '{}'::jsonb END
    ) AS field(key,value)
    WHERE field.key IN (
        'field:hydration_ml', 'field:sleep_score', 'field:sleep_seconds', 'field:deep_seconds',
        'field:rem_seconds', 'field:light_seconds', 'field:awake_seconds', 'field:hrv_nightly_avg',
        'field:hrv_weekly_avg', 'field:hrv_baseline_low', 'field:hrv_baseline_high', 'field:hrv_status',
        'field:resting_hr', 'field:body_battery_high', 'field:body_battery_low',
        'field:body_battery_charged', 'field:body_battery_drained', 'field:stress_avg', 'field:stress_max',
        'field:steps', 'field:active_calories', 'field:intensity_minutes', 'field:training_readiness_score',
        'field:recovery_time_minutes', 'field:training_status', 'field:vo2max'
    )
)
SELECT raw.id, raw.endpoint, raw.source_key, raw.parser_version, raw.status, raw.archive_key,
       raw.fetched_at,
       CASE WHEN raw.endpoint = 'activity_fit' THEN EXISTS (
           SELECT 1 FROM activities AS activity
           WHERE activity.id = raw.source_key
             AND (raw.id = failed.id OR raw.archive_key =
                   coalesce(activity.details->>'parsed_fit_key', activity.fit_key))
       ) ELSE coalesce(
           watermark.key IS NULL
           OR coalesce(failed.id::text, watermark.value->>'source_ref') IS NOT DISTINCT FROM raw.id::text
           OR EXISTS (SELECT 1 FROM projection_owner AS owner WHERE owner.reference = raw.id::text)
           OR (starts_with(raw.endpoint, 'activity_') AND raw.parser_version > 0
               AND NOT EXISTS (
                   SELECT 1 FROM app_state AS state
                   WHERE state.key = concat('activity-parts-owner:', raw.source_key, ':', raw.endpoint)
               )
               -- The existing selector considers an unknown ownership test
               -- ineligible for classification as superseded.
               AND ((
                   SELECT jsonb_agg(part.payload ORDER BY part.sequence) FROM activity_parts AS part
                   WHERE part.activity_id = raw.source_key AND part.kind = raw.endpoint
               ) = CASE WHEN jsonb_typeof(raw.payload) = 'array'
                        THEN raw.payload ELSE jsonb_build_array(raw.payload) END
               ) IS NOT FALSE
           )
           OR (
               raw.status IN ('normalized', 'partial', 'error') AND raw.parser_version > 0
               AND EXISTS (
                   SELECT 1 FROM activities AS activity
                   WHERE (
                       (raw.endpoint = 'activity' AND activity.id = raw.source_key)
                       OR (
                           raw.endpoint = 'activities' AND EXISTS (
                               SELECT 1 FROM jsonb_array_elements(
                                   CASE WHEN jsonb_typeof(raw.payload) = 'array'
                                        THEN raw.payload ELSE '[]'::jsonb END
                               ) AS entry(value)
                               WHERE entry.value->>'activityId' = activity.id
                           )
                       )
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM app_state AS state
                       WHERE state.key = concat('activity-version:', activity.id)
                         AND (state.value->>'owners_initialized')::boolean IS TRUE
                   )
               )
           ), false
       )
       END AS canonical
FROM source_page AS raw
LEFT JOIN app_state AS watermark ON watermark.key =
    concat('ingest:', raw.source, ':', raw.endpoint, ':', raw.source_key)
LEFT JOIN stream_failures AS failed ON failed.source = raw.source AND failed.endpoint = raw.endpoint
    AND failed.source_key = raw.source_key
ORDER BY raw.id
