SELECT id,start,"end",kind,status,revision,
    CASE WHEN kind='caffeine' THEN coalesce((
        SELECT jsonb_object_agg(key,value) FROM jsonb_each(payload)
        WHERE key IN ('dose_basis','servings','dose_provenance',
            'caffeine_mg_min','caffeine_mg_estimate','caffeine_mg_max')
    ),'{}'::jsonb)::text ELSE '{}' END AS payload
FROM events
WHERE NOT deleted AND status IN ('confirmed','needs_confirmation','inferred')
    AND (status<>'confirmed' OR source<>'inferred')
    AND kind IN ('caffeine','caffeine_absence','caffeine_log_complete','illness','travel')
    AND start < $2 AND ("end">=$1 OR start>=$1 OR (kind='illness' AND "end" IS NULL))
ORDER BY start,id LIMIT 1001
