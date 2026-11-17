WITH previous AS (
    SELECT value FROM app_state WHERE key='analysis:conversation'
), pending AS (
    DELETE FROM app_state WHERE starts_with(key,'analysis:conversation:pending')
    RETURNING value->'turn'->>'update_id' AS update_id
), retired AS (
    SELECT turn->>'update_id' AS update_id
    FROM previous CROSS JOIN LATERAL jsonb_array_elements(value->'turns') AS turn
    UNION ALL SELECT update_id FROM pending
), redact AS (
    UPDATE app_state AS reply
    SET value=reply.value || '{"text":"Контекст анализа удалён.","status":"forgotten",
                                "keyboard":false,"kind":"analysis"}'::jsonb,
        updated_at=now()
    WHERE starts_with(reply.key,'telegram:reply:') AND (
        reply.value->>'kind'='analysis'
        OR reply.key IN (SELECT 'telegram:reply:' || update_id FROM retired)
    )
)
INSERT INTO app_state(key,value)
VALUES('analysis:conversation',jsonb_build_object('epoch',gen_random_uuid()::text,'turns','[]'::jsonb))
ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()
