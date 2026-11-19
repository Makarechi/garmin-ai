SELECT jsonb_object_agg(key, safe)::text FROM (
    SELECT key,CASE
        WHEN key='integration:garmin' THEN jsonb_build_object(
            'status',left(value->>'status',64),
            'blocked_until',CASE
                WHEN value->'blocked_until' IS NULL OR value->'blocked_until'='null'::jsonb THEN NULL
                WHEN jsonb_typeof(value->'blocked_until')='string'
                    THEN to_jsonb(left(value->>'blocked_until',96))
                WHEN value->'blocked_until' IN ('false'::jsonb,'0'::jsonb,'[]'::jsonb,'{}'::jsonb)
                    THEN 'false'::jsonb
                ELSE 'true'::jsonb
            END)
        WHEN key='storage:backup-capacity' THEN jsonb_build_object(
            'at',left(value->>'at',96),'status',left(value->>'status',64),
            'volumes',CASE
                WHEN jsonb_typeof(value->'volumes')='array' THEN
                    CASE WHEN jsonb_array_length(value->'volumes') BETWEEN 1 AND 2 THEN
                        (SELECT jsonb_agg(jsonb_build_object(
                            'role',left(item->>'role',64),
                            'free_bytes',CASE WHEN jsonb_typeof(item->'free_bytes')='number'
                                             THEN item->'free_bytes' END,
                            'required_bytes',CASE WHEN jsonb_typeof(item->'required_bytes')='number'
                                                 THEN item->'required_bytes' END))
                         FROM jsonb_array_elements(value->'volumes') item)
                    END
            END)
        ELSE jsonb_build_object('at',left(value->>'at',96))
    END AS safe
    FROM app_state WHERE key IN (
        'integration:garmin','storage:backup-capacity','runtime:heartbeat','backup:last_success'
    )
) data
