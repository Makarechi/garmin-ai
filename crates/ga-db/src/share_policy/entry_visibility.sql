FROM events entry
LEFT JOIN event_definition_versions version ON version.id=entry.definition_version_id
CROSS JOIN destination
LEFT JOIN app_state AS consent ON consent.key=concat(
    'tracker-consent:',version.definition_id,':',destination.kind,':',destination.instance)
WHERE NOT entry.deleted
  AND (entry.definition_version_id IS NULL OR version.allowed_operations ? 'query')
  AND (NOT starts_with(entry.kind,'user.') OR
       (version.id IS NOT NULL AND (version.privacy='private' OR (
