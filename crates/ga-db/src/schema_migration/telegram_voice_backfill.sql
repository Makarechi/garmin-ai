UPDATE public.inbound_messages AS inbound
SET kind = 'voice',
    normalized_text = COALESCE(
        updates.payload #>> '{message,text}',
        updates.payload #>> '{message,caption}'
    ),
    envelope = inbound.envelope || jsonb_build_object(
        'attachments', jsonb_build_array(jsonb_build_object(
            'kind', 'voice',
            'external_id', updates.payload #>> '{message,voice,file_id}',
            'media_type', COALESCE(updates.payload #>> '{message,voice,mime_type}', 'audio/ogg'),
            'size_bytes', updates.payload #> '{message,voice,file_size}'
        ))
    )
FROM public.telegram_updates AS updates
WHERE inbound.legacy_telegram_update_id = updates.id
  AND updates.payload #>> '{message,voice,file_id}' IS NOT NULL;
