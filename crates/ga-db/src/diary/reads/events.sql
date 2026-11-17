SELECT row_to_json(event)::text, event.start, event."end", event.created_at,
       event.updated_at, event.recorded_at, event.ingested_at,
       event.kind LIKE 'user.%' AND COALESCE(version.allowed_operations ? 'update', false)
       AS can_update
FROM events AS event
LEFT JOIN event_definition_versions AS version ON version.id = event.definition_version_id
WHERE NOT event.deleted
  AND (event.definition_version_id IS NULL OR version.allowed_operations @> '["query"]'::jsonb)
  AND ($3::text IS NULL OR event.kind = $3)
  AND event.start < $2
  AND (
      event."end" > $1
      OR (event."end" IS NULL AND event.topology = 'open_interval')
      OR ((event."end" IS NULL OR event."end" = event.start)
          AND event.topology IN ('point', 'flexible') AND event.start >= $1)
  )
ORDER BY (event.start >= $1) DESC, event.start, event.id
LIMIT $4
