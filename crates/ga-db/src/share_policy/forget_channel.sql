WITH previous AS (
    SELECT COALESCE((SELECT value FROM app_state WHERE key='analysis:conversation'),'{}'::jsonb)
        AS value
), turns AS (
    SELECT turn,ordinality,
           CASE WHEN turn ? 'channel_instance_id' THEN turn->>'channel_instance_id'
                ELSE 'telegram:primary' END AS channel_instance
    FROM previous CROSS JOIN LATERAL jsonb_array_elements(value->'turns') WITH ORDINALITY AS item(turn,ordinality)
), pending AS (
    DELETE FROM app_state WHERE key=$2 AND
        CASE WHEN (value->'turn') ? 'channel_instance_id'
             THEN value->'turn'->>'channel_instance_id'
             ELSE 'telegram:primary' END = $1
    RETURNING value->'turn'->>'update_id' AS update_id
), retired AS (
    SELECT turn->>'update_id' AS update_id FROM turns WHERE channel_instance=$1
    UNION ALL SELECT update_id FROM pending
), redact AS (
    UPDATE app_state AS reply
    SET value=reply.value || '{"text":"Контекст анализа удалён.","status":"forgotten",
                                "keyboard":false,"kind":"analysis"}'::jsonb,
        updated_at=now()
    WHERE starts_with(reply.key,'telegram:reply:') AND (
        reply.key IN (SELECT 'telegram:reply:' || update_id FROM retired)
        OR (reply.value->>'kind'='analysis'
            AND COALESCE(reply.value->>'channel_instance_id','telegram:primary')=$1)
    )
)
INSERT INTO app_state(key,value)
SELECT 'analysis:conversation',value || jsonb_build_object(
    'epochs', COALESCE(value->'epochs','{}'::jsonb) || jsonb_build_object($1::text,gen_random_uuid()::text),
    'turns', COALESCE((SELECT jsonb_agg(turn ORDER BY ordinality) FROM turns
                      WHERE channel_instance IS DISTINCT FROM $1),'[]'::jsonb)
)
FROM previous
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()
