SELECT (
        (to_jsonb(event)-'original_text'-'idempotency_key')
        || jsonb_build_object(
            'notes_truncated',coalesce(length(payload->>'notes')>2000,false),
            'payload',payload || CASE WHEN length(payload->>'notes')>2000
                THEN jsonb_build_object('notes',left(payload->>'notes',2000))
                ELSE '{}'::jsonb END
        )
    )::text,
    start,"end",created_at,updated_at,recorded_at,ingested_at
FROM events event
WHERE kind='wellbeing_observation' AND NOT deleted AND status='confirmed'
    AND source NOT IN ('inferred','wearable') AND start >= $1 AND start < $2
    AND ($3::timestamptz IS NULL OR (start,id)>($3,$4::uuid))
ORDER BY start,id LIMIT 201
