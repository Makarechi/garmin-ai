WITH page AS (
    SELECT kind, sequence
    FROM activity_parts
    WHERE activity_id = $1 AND (
        $2 OR (kind <> 'activity_details' AND (kind NOT LIKE 'fit_%' OR kind = ANY($5::text[])))
    )
    ORDER BY kind, sequence
    LIMIT $4 + 1 OFFSET $3
), ranked AS (
    SELECT kind, sequence, row_number() OVER (ORDER BY kind, sequence) <= $4 AS wanted
    FROM page
)
SELECT CASE WHEN octet_length(document) > $6 THEN '' ELSE document END
FROM ranked LEFT JOIN LATERAL (
    SELECT row_to_json(part)::text AS document FROM (
        SELECT activity_id, kind, sequence, payload
        FROM activity_parts
        WHERE ranked.wanted AND activity_id = $1 AND kind = ranked.kind AND sequence = ranked.sequence
    ) AS part
) AS part ON TRUE
ORDER BY ranked.kind, ranked.sequence
