            ) IS TRUE
        )) AS permitted
    FROM event_definition_versions AS version CROSS JOIN destinations AS destination
    GROUP BY version.id,destination.categories
), schema_refs AS (
    SELECT NULL::uuid AS id WHERE FALSE
), rows AS (
    SELECT 'source.raw_only'::text AS key,jsonb_build_object(
        'id','source.raw_only','kind','source','availability','raw_only','operations','[]'::jsonb
    ) AS document
    WHERE EXISTS(SELECT FROM source_payloads WHERE starts_with(source,'plugin:') AND status='raw_only')
        AND ($1::text IS NULL OR $1<'source.raw_only')
        AND ($2::text IS NULL OR $2='source.raw_only')
