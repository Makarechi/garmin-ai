WITH destination(kind,instance,categories) AS (VALUES ($2::text,$3::text,to_jsonb($4::text[])))
SELECT EXISTS (
    SELECT 1 FROM event_definition_versions AS version CROSS JOIN destination
    LEFT JOIN app_state AS consent ON consent.key=concat(
        'tracker-consent:', version.definition_id, ':', destination.kind, ':', destination.instance)
    WHERE version.id=$1
      AND ((version.privacy='private' AND NOT (destination.categories ? 'original_text')) OR (
