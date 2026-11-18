WITH destination(kind, instance, categories) AS (
    VALUES ('channel'::text, $1::text, '["schema","facts"]'::jsonb)
)
SELECT entry.id, entry.revision, entry.kind, entry.payload::text AS payload,
       entry.start, entry."end" AS ended_at, entry.topology,
       to_char(timezone(entry.timezone,entry.start), 'DD.MM.YYYY HH24:MI') AS display_start,
       version.definition_id, version.id AS version_id,
       coalesce(version.allowed_operations ? 'update', version.id IS NULL) AS can_update,
       coalesce(version.allowed_operations ? 'delete', version.id IS NULL) AS can_delete
FROM events entry
LEFT JOIN event_definition_versions version ON version.id=entry.definition_version_id
CROSS JOIN destination
LEFT JOIN app_state AS consent ON consent.key=concat(
    'tracker-consent:',version.definition_id,':',destination.kind,':',destination.instance)
WHERE NOT entry.deleted
  AND (entry.definition_version_id IS NULL OR version.allowed_operations ? 'query')
  AND (NOT starts_with(entry.kind,'user.') OR
       (version.id IS NOT NULL AND (version.privacy='private' OR (
