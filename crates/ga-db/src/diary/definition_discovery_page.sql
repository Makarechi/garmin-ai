                ) IS TRUE
            )
        )) AS permitted
    FROM event_definition_versions AS version
), selected AS (
    SELECT definition.*,current.document AS contract
    FROM event_definitions AS definition
    LEFT JOIN versions AS current ON current.definition_id=definition.id
        AND current.version=definition.current_version
    WHERE ($1::text IS NULL OR definition.key>$1)
      AND ($2::text IS NULL OR definition.key=$2)
      AND ($5 IS NULL OR definition.namespace<>'user' OR current.permitted)
    ORDER BY definition.key LIMIT $4+1
), numbered AS (
    SELECT selected.*,row_number() OVER (ORDER BY key) AS position FROM selected
), rows AS (
    SELECT definition.key,json_build_object(
        'id',definition.id,'key',definition.key,'namespace',definition.namespace,
        'status',definition.status,'revision',definition.revision,
        'current_version',definition.current_version,'contract',definition.contract,
        'versions',COALESCE((
            SELECT json_agg(history.document ORDER BY history.version)
                FILTER (WHERE history.position<=5 AND
                    ($5 IS NULL OR definition.namespace<>'user' OR history.permitted))
            FROM (
                SELECT versions.*,row_number() OVER (ORDER BY version DESC) AS position
                FROM versions WHERE definition_id=definition.id
                  AND ($3::text IS NULL OR version::numeric<$3::numeric)
                ORDER BY version DESC LIMIT 6
            ) AS history
        ),'[]'::json),
        'versions_before',(
            SELECT CASE WHEN count(*)>5 THEN min(version) FILTER (WHERE position<=5) END
            FROM (
                SELECT version,row_number() OVER (ORDER BY version DESC) AS position
                FROM event_definition_versions WHERE definition_id=definition.id
                  AND ($3::text IS NULL OR version::numeric<$3::numeric)
                ORDER BY version DESC LIMIT 6
            ) AS history
        )
    ) AS document
    FROM numbered AS definition WHERE position<=$4
), document AS (
    SELECT COALESCE((SELECT json_agg(document ORDER BY key) FROM rows),'[]'::json) AS content,
        (SELECT key FROM numbered WHERE position=$4
            AND EXISTS (SELECT 1 FROM numbered WHERE position>$4))
            AS next_cursor
), source AS MATERIALIZED (
    SELECT convert_to(CASE WHEN $6::boolean THEN content
        ELSE json_build_object('rows',content,'next_cursor',next_cursor)
        END::text,'UTF8') AS bytes,next_cursor
    FROM document
)
SELECT substring(bytes FROM byte_offset FOR 65536),next_cursor
FROM source CROSS JOIN LATERAL generate_series(1,octet_length(bytes),65536) AS parts(byte_offset)
ORDER BY byte_offset
