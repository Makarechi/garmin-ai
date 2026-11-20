), numbered AS (
    SELECT key,document,row_number() OVER (ORDER BY key) AS position FROM rows
), source AS MATERIALIZED (
    SELECT convert_to(json_build_object(
        'resources',COALESCE((SELECT json_agg(document ORDER BY key)
            FROM numbered WHERE position<=$3),'[]'::json),
        'next_cursor',(SELECT key FROM numbered WHERE position=$3
            AND EXISTS(SELECT FROM numbered WHERE position>$3))
    )::text,'UTF8') AS bytes,
    ARRAY(SELECT id FROM schema_refs ORDER BY id LIMIT 4097) AS schema_versions
    WHERE $4::boolean IS NOT NULL AND $6::uuid IS NOT NULL AND $7::boolean IS NOT NULL
        AND $8::text[] IS NOT NULL AND $9::text[] IS NOT NULL AND $10::text IS NOT NULL AND ($11::text IS NULL OR $5::text IS NOT NULL)
)
SELECT substring(bytes FROM byte_offset FOR 65536),
    CASE WHEN byte_offset=1 THEN schema_versions ELSE ARRAY[]::uuid[] END
FROM source CROSS JOIN LATERAL generate_series(1,octet_length(bytes),65536) AS parts(byte_offset)
ORDER BY byte_offset
