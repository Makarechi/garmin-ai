WITH allowed(kind, keys) AS (VALUES
    ('fit_device_info', ARRAY['device_index','device_type','manufacturer','software_version',
                              'hardware_version','sensor_position','source_type']),
    ('fit_hr_zone', ARRAY['high_bpm','message_index']),
    ('fit_zones_target', ARRAY['max_heart_rate','threshold_heart_rate','functional_threshold_power',
                               'hr_calc_type','pwr_calc_type']),
    ('fit_ohr_settings', ARRAY['enabled']),
    ('fit_hrm_profile', ARRAY['enabled','log_hrv','message_index'])
), selected AS MATERIALIZED (
    SELECT part.kind, part.sequence, part.payload, allowed.keys,
           row_number() OVER(ORDER BY part.sequence,part.kind) AS position
    FROM activity_parts AS part JOIN allowed ON allowed.kind=part.kind
    WHERE part.activity_id=$1
    ORDER BY part.sequence,part.kind
    LIMIT $2 + 1
)
SELECT kind, sequence,
       CASE WHEN position <= $2 THEN (
           SELECT COALESCE(jsonb_object_agg(key,value),'{}'::jsonb)::text
           FROM unnest(keys) AS key
           CROSS JOIN LATERAL (SELECT payload->key AS value) AS field
           WHERE jsonb_typeof(value)='boolean'
              OR (jsonb_typeof(value)='number' AND length(value::text) <= 4301)
              OR (jsonb_typeof(value)='string' AND length(value#>>'{}')<=80)
       ) END
FROM selected
ORDER BY position
