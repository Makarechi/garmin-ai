WITH identities AS (SELECT DISTINCT id FROM unnest($1::text[]) AS activities(id)),
     endpoints AS (
        SELECT unnest(ARRAY['activity','activity_details','activity_splits',
                            'activity_typed_splits','activity_zones','activity_weather']) AS endpoint
     ),
     clock AS (SELECT $4::timestamptz AS at, ($4::timestamptz AT TIME ZONE 'UTC')::date AS day)
INSERT INTO jobs(id,kind,payload,dedup_key,status,run_at,attempts)
SELECT gen_random_uuid(),'garmin_endpoint',
       jsonb_build_object('endpoint', endpoint, 'key', id, 'account', $2::text, 'backfill', $3::bool),
       format('activity:%s:%s:%s:%s:%s',$2,id,endpoint,delay,day),
       'pending',at+delay*interval '1 second',0
FROM identities CROSS JOIN endpoints CROSS JOIN (VALUES (0),(1200)) AS refresh(delay)
CROSS JOIN clock
UNION ALL
SELECT gen_random_uuid(),'garmin_fit',
       jsonb_build_object('activity_id',id,'account',$2::text,'backfill',$3::bool),
       format('fit:%s:%s:%s',$2,id,day),'pending',at,0
FROM identities CROSS JOIN clock
ON CONFLICT (dedup_key) DO NOTHING
