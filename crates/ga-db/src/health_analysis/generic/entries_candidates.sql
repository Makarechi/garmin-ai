WITH versions AS (SELECT id FROM event_definition_versions WHERE definition_id=$1)
SELECT event.id FROM events AS event
WHERE (
    event.definition_version_id IN (SELECT id FROM versions)
    OR EXISTS(SELECT FROM audit_log AS audit WHERE audit.event_id=event.id AND (
        audit.before->>'definition_version_id' IN (SELECT id::text FROM versions)
        OR audit.after->>'definition_version_id' IN (SELECT id::text FROM versions)
    ))
) AND (
    CASE WHEN $4='starts_within' THEN event.start>=$2 AND event.start<$3 ELSE
        event.start<$3 AND (event."end">$2
            OR (event."end" IS NULL AND (event.topology='open_interval'
                OR (event.topology IS NULL AND event.kind IN ('migraine','illness'))))
            OR (event.start>=$2 AND (event.topology IN ('point','flexible') OR event.topology IS NULL)
                AND (event."end" IS NULL OR event."end"=event.start))) END
    OR EXISTS (
        SELECT FROM audit_log AS audit
        CROSS JOIN LATERAL (VALUES (audit.before),(audit.after)) AS snapshot(value)
        WHERE audit.event_id=event.id AND CASE WHEN $4='starts_within' THEN
            (snapshot.value->>'start')::timestamptz>=$2 AND (snapshot.value->>'start')::timestamptz<$3
        ELSE (snapshot.value->>'start')::timestamptz<$3 AND (
            (snapshot.value->>'end')::timestamptz>$2
            OR (snapshot.value->>'end' IS NULL AND (snapshot.value->>'topology'='open_interval'
                OR (snapshot.value->>'topology' IS NULL
                    AND snapshot.value->>'kind' IN ('migraine','illness'))))
            OR ((snapshot.value->>'start')::timestamptz>=$2
                AND (snapshot.value->>'topology' IN ('point','flexible') OR snapshot.value->>'topology' IS NULL)
                AND (snapshot.value->>'end' IS NULL
                    OR (snapshot.value->>'end')::timestamptz=(snapshot.value->>'start')::timestamptz))
        ) END
    )
)
ORDER BY event.id LIMIT 10001
