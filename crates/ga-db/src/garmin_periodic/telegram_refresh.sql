-- Inspect only recent native messages. Delayed Telegram deliveries cannot
-- request fresh data on behalf of a conversation that has already ended.
WITH opener AS MATERIALIZED (
    SELECT incoming.id
    FROM inbound_messages AS incoming
    JOIN jobs AS delivery ON delivery.dedup_key=concat('telegram:inbound:native:',incoming.id)
    WHERE incoming.owner_id=$1 AND incoming.channel='telegram'
      AND incoming.kind IN ('text','voice')
      AND NOT starts_with(ltrim(coalesce(incoming.normalized_text,'')), '/')
      AND incoming.received_at BETWEEN $3::timestamptz-interval '2 minutes' AND $3
      AND coalesce(incoming.occurred_at,incoming.received_at)>=$3::timestamptz-interval '2 minutes'
      AND delivery.kind='telegram_native_inbound'
      AND NOT EXISTS (
          SELECT 1 FROM inbound_messages AS prior
          WHERE prior.conversation_id=incoming.conversation_id AND prior.kind IN ('text','voice')
            AND NOT starts_with(ltrim(coalesce(prior.normalized_text,'')), '/')
            AND coalesce(prior.occurred_at,prior.received_at)
                > coalesce(incoming.occurred_at,incoming.received_at)-interval '15 minutes'
            AND (coalesce(prior.occurred_at,prior.received_at),prior.received_at,prior.id)
                < (coalesce(incoming.occurred_at,incoming.received_at),incoming.received_at,incoming.id)
      )
    ORDER BY incoming.received_at DESC,incoming.id DESC
    LIMIT 1
)
INSERT INTO jobs (id,kind,payload,dedup_key,status,run_at,attempts)
SELECT gen_random_uuid(),'garmin_endpoint',jsonb_build_object('endpoint',data.endpoint,'key',$2::text),
       concat('telegram-refresh:',opener.id,':',data.endpoint),'pending',$3,0
FROM opener CROSS JOIN (VALUES ('sleep'),('daily')) AS data(endpoint)
WHERE data.endpoint=ANY($4::text[])
  AND NOT EXISTS (
      SELECT 1 FROM jobs AS existing
      WHERE existing.kind='garmin_endpoint' AND existing.status IN ('pending','running')
        AND existing.payload->>'endpoint'=data.endpoint AND existing.payload->>'key'=$2
  )
  AND NOT EXISTS (
      SELECT 1 FROM app_state AS freshness
      CROSS JOIN LATERAL (VALUES (freshness.value->>'fetched_at'),
                                (freshness.value->'latest_attempt'->>'fetched_at')) AS observed(at)
      WHERE freshness.key=concat('ingest:garmin_connect:',data.endpoint,':',$2)
        AND CASE WHEN pg_input_is_valid(observed.at,'timestamptz')
                 THEN observed.at::timestamptz>=$3::timestamptz-interval '3 minutes' ELSE false END
  )
ON CONFLICT(dedup_key) DO NOTHING
