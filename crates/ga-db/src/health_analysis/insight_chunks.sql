WITH source AS MATERIALIZED (
    SELECT convert_to(json_build_object(
        'id',id,'category',category,'statement',statement,'evidence',evidence,
        'sample_size',sample_size,'effect_size',$3::json,'status',status,
        'dedup_key',dedup_key,'generated_at',$2::text
    )::text,'UTF8') AS bytes
    FROM insights WHERE id=$1
)
SELECT substring(bytes FROM byte_offset FOR 65536)
FROM source CROSS JOIN LATERAL generate_series(1,octet_length(bytes),65536) AS parts(byte_offset)
ORDER BY byte_offset
