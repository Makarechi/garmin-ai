WITH destination(kind,instance,categories) AS (
    VALUES ('model'::text,$5::text,'["schema"]'::jsonb),
           ('channel'::text,$7::text,'["schema"]'::jsonb)
), versions AS NOT MATERIALIZED (
    SELECT version.*,
        json_build_object(
            'id',version.id,'definition_id',version.definition_id,'version',version.version,
            'schema',version.schema,'schema_hash',version.schema_hash,
            'topology',version.topology,'fields',version.field_metadata,'labels',version.labels,
            'privacy',version.privacy,'allowed_operations',version.allowed_operations
        ) AS document,
        ($5 IS NULL OR version.privacy<>'sensitive' OR NOT EXISTS (
            SELECT 1 FROM destination WHERE destination.instance IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM app_state AS consent WHERE consent.key=concat(
                    'tracker-consent:',version.definition_id,':',destination.kind,':',destination.instance
                ) AND (
