NOT EXISTS (
    SELECT 1 FROM inbound_messages earlier
    JOIN jobs previous ON previous.dedup_key=concat('telegram:inbound:native:',earlier.id)
     AND previous.payload->>'inbound_message_id'=earlier.id::text
    WHERE earlier.owner_id=i.owner_id AND earlier.channel=i.channel
      AND earlier.channel_instance_id=i.channel_instance_id
      AND earlier.status='pending' AND earlier.legacy_telegram_update_id IS NULL
      AND previous.kind=j.kind AND previous.status IN ('pending','running')
      AND coalesce(previous.payload->>'lane','diary')='diary'
      AND (coalesce((previous.payload->>'ordering_epoch')::bigint,0),
        earlier.external_event_id::bigint,previous.id)
        < (coalesce((j.payload->>'ordering_epoch')::bigint,0),i.external_event_id::bigint,j.id)
)
