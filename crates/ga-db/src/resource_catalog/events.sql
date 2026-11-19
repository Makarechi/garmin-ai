            ) IS TRUE
        )) AS permitted
    FROM event_definition_versions AS version CROSS JOIN destinations AS destination
), selected AS (
    SELECT definition.key,definition.namespace,version.*,facts.permitted AS facts_allowed
    FROM event_definitions AS definition JOIN event_definition_versions AS version
        ON version.definition_id=definition.id AND version.version=definition.current_version
    JOIN permissions AS schema ON schema.id=version.id AND schema.category='schema'
    JOIN permissions AS facts ON facts.id=version.id AND facts.category='facts'
    WHERE definition.status='active'
        AND ($1::text IS NULL OR definition.key>$1)
        AND ($2::text IS NULL OR definition.key=$2)
        AND ($5 IS NULL OR definition.namespace='system' OR schema.permitted)
        AND ($5 IS NULL OR definition.namespace<>'system'
            OR NOT (regexp_replace(definition.key,'^system[.]','')=ANY($8::text[])))
    ORDER BY definition.key LIMIT $3+1
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
