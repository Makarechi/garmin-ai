WITH destination(kind, instance, categories) AS (
    VALUES ('channel'::text, $1::text, '["schema","facts"]'::jsonb)
)
SELECT entry.id, entry.revision, entry.kind, entry.payload::text AS payload,
       entry.start, entry."end" AS ended_at, entry.topology,
       to_char(timezone(entry.timezone,entry.start), 'DD.MM.YYYY HH24:MI') AS display_start,
       version.definition_id, version.id AS version_id,
       coalesce(version.allowed_operations ? 'update', version.id IS NULL) AS can_update,
       coalesce(version.allowed_operations ? 'delete', version.id IS NULL) AS can_delete
