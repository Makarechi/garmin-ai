consent.value->>'definition_id' = version.definition_id::text
AND consent.value->>'destination_kind' = destination.kind
AND consent.value->>'destination_instance_id' = destination.instance
AND jsonb_typeof(consent.value->'categories') = 'array'
AND consent.value->'categories' @> destination.categories
AND consent.value->'categories' <@ '["schema","facts","original_text"]'::jsonb
AND (NOT (consent.value ? 'policy_revision')
    OR (jsonb_typeof(consent.value->'policy_revision')='number'
        AND consent.value->>'policy_revision' = '1'))
AND CASE WHEN jsonb_typeof(consent.value)='object' THEN consent.value - ARRAY[
    'definition_id','destination_kind','destination_instance_id',
    'categories','granted_at','policy_revision'
]::text[] = '{}'::jsonb ELSE false END
AND CASE WHEN consent.value->>'granted_at'
            ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?([Zz]|[+-][0-9]{2}:[0-9]{2})$'
            AND pg_input_is_valid(consent.value->>'granted_at','timestamp with time zone')
         THEN (consent.value->>'granted_at')::timestamptz <= clock_timestamp()
         ELSE false
    END
