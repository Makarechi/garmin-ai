            ) IS TRUE
        )) AS permitted
    FROM event_definition_versions AS version CROSS JOIN destinations AS destination
    GROUP BY version.id,destination.categories
), visible_definitions AS NOT MATERIALIZED (
    SELECT definition.id,definition.key,definition.namespace,definition.status,
        current.id AS current_id
    FROM event_definitions AS definition LEFT JOIN event_definition_versions AS current
        ON current.definition_id=definition.id AND current.version=definition.current_version
    LEFT JOIN permissions AS schema ON schema.id=current.id AND schema.category='schema'
    WHERE ($1::text IS NULL OR definition.key>$1)
        AND ($2::text IS NULL OR definition.key=$2)
        AND ($5 IS NULL OR definition.namespace<>'user' OR schema.permitted)
), selected AS MATERIALIZED (
    SELECT definition.key,definition.namespace,version.*,facts.permitted AS facts_allowed
    FROM visible_definitions AS definition
    JOIN event_definition_versions AS version ON version.id=definition.current_id
    JOIN permissions AS facts ON facts.id=version.id AND facts.category='facts'
    WHERE definition.status='active'
        AND ($5 IS NULL OR definition.namespace<>'system'
            OR NOT (regexp_replace(definition.key,'^system[.]','')=ANY($8::text[])))
    ORDER BY definition.key LIMIT $3+1
), definition_pages AS (
    SELECT *,row_number() OVER (ORDER BY key) AS position
    FROM visible_definitions WHERE $11::text IS NOT NULL
), consulted_definitions AS (
    -- The legacy tool reads whole pages of 50 contracts before filtering the catalog.
    SELECT id,current_id
    FROM definition_pages
    WHERE namespace='user' AND position<=COALESCE((
        SELECT ((position+49)/50)*50 FROM definition_pages
        WHERE key=(SELECT key FROM selected ORDER BY key OFFSET $3 LIMIT 1)
    ), 9223372036854775807)
), schema_refs AS (
    SELECT current_id AS id FROM consulted_definitions
    UNION
    SELECT version.id
    FROM consulted_definitions AS definition
    CROSS JOIN LATERAL (
        SELECT id FROM event_definition_versions
        WHERE definition_id=definition.id ORDER BY version DESC LIMIT 5
    ) AS version
    JOIN permissions AS schema ON schema.id=version.id AND schema.category='schema'
        AND schema.permitted
), rows AS (
    SELECT selected.key,
        jsonb_build_object('id',key,'version',version,'version_id',id,'kind','event',
            'labels',labels,'time_semantics',topology,
            'operations',CASE WHEN allowed_operations ? 'query' THEN '["query"]'::jsonb ELSE '[]'::jsonb END,
            'availability',CASE WHEN facts_allowed OR namespace='system' THEN (
                SELECT jsonb_build_object('records_for_version',count(*),
                    'latest_start',to_char(max(start) AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS') ||
                        CASE WHEN extract(microseconds FROM max(start))::bigint%1000000=0
                            THEN '' ELSE to_char(max(start) AT TIME ZONE 'UTC','.US') END || '+00:00',
                    'quality','nondeleted entries; status and completeness may vary')
                FROM events WHERE definition_version_id=selected.id AND NOT deleted
            ) END
        ) || CASE WHEN $4 THEN jsonb_build_object('fields',field_metadata,'schema_hash',schema_hash)
            ELSE '{}'::jsonb END AS document
    FROM selected
