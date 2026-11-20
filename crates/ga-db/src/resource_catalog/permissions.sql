WITH destinations(kind,instance,categories) AS (VALUES
    ('model'::text,$5::text,'["schema"]'::jsonb),
    ('model'::text,$5::text,'["facts"]'::jsonb),
    ('channel'::text,$11::text,'["schema"]'::jsonb),
    ('channel'::text,$11::text,'["facts"]'::jsonb)
), permissions AS NOT MATERIALIZED (
    SELECT version.id,destination.categories->>0 AS category,
        bool_and(destination.instance IS NULL OR version.privacy='private' OR EXISTS (
            SELECT 1 FROM app_state AS consent WHERE consent.key=concat(
                'tracker-consent:',version.definition_id,':',destination.kind,':',destination.instance)
            AND (
