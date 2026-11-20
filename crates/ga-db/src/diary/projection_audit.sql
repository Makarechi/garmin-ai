WITH expected AS (
    SELECT * FROM jsonb_to_recordset($2::text::jsonb) AS field(
        field_id text, metric_version uuid, metric text,
        number double precision, label text, flag boolean, unit text, sequence integer
    )
), generations AS (
    SELECT field_id, count(*) FILTER (WHERE valid) AS valid,
        count(*) AS generations, count(DISTINCT projection_version) AS distinct_generations,
        min(projection_version) AS first_generation, max(projection_version) AS last_generation
    FROM metric_observations WHERE source_entry_id = $1 GROUP BY field_id
), matches AS (
    SELECT expected.field_id, coalesce(generations.valid, 0) AS valid,
           generations.generations = generations.distinct_generations
           AND generations.first_generation = 1
           AND generations.last_generation = generations.generations
           AND EXISTS (
               SELECT 1 FROM metric_observations actual JOIN events event ON event.id = $1
               WHERE actual.source_entry_id = $1 AND actual.field_id = expected.field_id AND actual.valid
                 AND actual.projection_version = generations.generations
                 AND ROW(actual.metric, actual.metric_definition_version_id, actual.value,
                         actual.value_text, actual.value_boolean, actual.unit, actual.source_ref,
                         actual.observed_at, actual.effective_start, actual.effective_end,
                         actual.recorded_at, actual.sequence, actual.timezone,
                         actual.source_calendar_date, actual.quality, actual.feature_version,
                         actual.account, actual.device, actual.precision, actual.coverage)
                     IS NOT DISTINCT FROM
                     ROW(expected.metric, expected.metric_version, expected.number,
                         expected.label, expected.flag, expected.unit, event.id,
                         event.start, event.start, event."end", event.recorded_at, expected.sequence,
                         event.timezone, $3::date,
                         'observed'::text, 'event-projection-v1'::text, NULL, NULL, NULL, NULL)
           ) AS correct
    FROM expected LEFT JOIN generations USING (field_id)
)
SELECT
    (SELECT coalesce(sum(valid), 0)::bigint FROM generations),
    (SELECT count(*) FROM matches WHERE valid = 0),
    (SELECT coalesce(sum(valid), 0)::bigint FROM generations
        WHERE NOT EXISTS (SELECT 1 FROM expected WHERE expected.field_id = generations.field_id)),
    (SELECT count(*) FROM matches WHERE valid > 0 AND (valid <> 1 OR correct IS DISTINCT FROM true)),
    (SELECT count(*) = 0 OR count(*) <> count(DISTINCT created_at)
         OR coalesce(bool_or(after IS NULL OR NOT (after ? 'status')), false)
     FROM audit_log WHERE event_id = $1)
