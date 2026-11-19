SELECT EXISTS (
    SELECT 1 FROM events AS event
    LEFT JOIN event_definition_versions AS version ON version.id=event.definition_version_id
    WHERE NOT event.deleted
      AND (event.definition_version_id IS NULL OR version.allowed_operations ? 'query')
      AND event.start<$2
      AND (event."end">$1
        OR (event."end" IS NULL AND event.topology='open_interval')
        OR ((event."end" IS NULL OR event."end"=event.start)
            AND event.topology IN ('point','flexible') AND event.start>=$1))
      AND NOT (event.kind=ANY($3::text[]))
      AND (NOT starts_with(event.kind,'user.') OR (version.id IS NOT NULL AND (
          version.privacy<>'sensitive' OR EXISTS (
              SELECT 1 FROM app_state AS consent
              WHERE consent.key=concat('tracker-consent:',version.definition_id,':model:', $4::text)
                AND consent.value->'categories' @> '["facts"]'::jsonb
          )
      )))
    OFFSET 500 LIMIT 1
)
