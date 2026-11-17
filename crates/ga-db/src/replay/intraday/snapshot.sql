CREATE TEMP TABLE ga_replay_sample_targets ON COMMIT DROP AS
SELECT measurements.*,
       false AS unknown_order,
       NULL::date AS restored_day,
       NULL::float8 AS restored_value,
       NULL::text AS restored_unit,
       NULL::timestamptz AS restored_at
FROM measurements
WHERE source_ref=$1
