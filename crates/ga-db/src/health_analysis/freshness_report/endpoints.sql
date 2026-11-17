WITH eligible AS (
    SELECT key, value, split_part(key, ':', 2) AS endpoint,
           (value->>'source_key' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
              AND value->>'source_key' <> $1) IS TRUE AS historical,
           COALESCE(value->>'fetched_at', value->>'success_at')::timestamptz AS fetched
    FROM app_state
    WHERE starts_with(key, 'freshness:')
      AND COALESCE(value->>'fetched_at', value->>'success_at') IS NOT NULL
), latest AS (
    SELECT DISTINCT ON (endpoint, historical) endpoint, historical, value
    FROM eligible
    ORDER BY endpoint, historical, fetched DESC, key
)
SELECT latest.endpoint, latest.historical, latest.value::text, raw.id,
       raw.source_updated_at, raw.payload_hash, raw.parser_version, raw.status,
       GREATEST(0, EXTRACT(EPOCH FROM
           ($2::timestamptz - COALESCE(NULLIF(latest.value->>'fetched_at', ''),
                                      latest.value->>'success_at')::timestamptz)))::float8 AS fetch_lag,
       CASE WHEN NULLIF(latest.value->>'success_at', '') IS NOT NULL
            THEN GREATEST(0, EXTRACT(EPOCH FROM
                   ($2::timestamptz - (latest.value->>'success_at')::timestamptz)))::float8
       END AS success_lag
FROM latest LEFT JOIN source_payloads AS raw ON raw.id = NULLIF(latest.value->>'source_ref', '')::uuid
