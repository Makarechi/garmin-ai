WITH bounds AS (SELECT 'epoch'::timestamptz+$2::interval AS start,
    'epoch'::timestamptz+$3::interval AS finish,'epoch'::timestamptz+$4::interval AS cutoff,
    'epoch'::timestamptz+$5::interval AS predecessor), eligible AS (
    SELECT observation.* FROM metric_observations AS observation CROSS JOIN bounds
    LEFT JOIN events AS event ON event.id=observation.source_entry_id
    LEFT JOIN event_definition_versions AS version ON version.id=event.definition_version_id
    LEFT JOIN LATERAL (SELECT after FROM audit_log WHERE event_id=event.id AND created_at<=bounds.cutoff
        ORDER BY created_at DESC,id DESC LIMIT 1) AS audit ON true
    WHERE observation.metric_definition_version_id=$1
      AND (observation.valid OR observation.invalidated_at>bounds.cutoff)
      AND observation.quality='observed' AND observation.ingested_at<=bounds.cutoff
      AND observation.observed_at<=bounds.cutoff
      AND ($6='all' OR ($6='event' AND observation.source_entry_id IS NOT NULL)
          OR ($6='observation' AND observation.source_entry_id IS NULL
              AND observation.account IS NOT DISTINCT FROM $7::text
              AND observation.device IS NOT DISTINCT FROM $8::text))
      AND (observation.source_entry_id IS NULL OR (
          (event.definition_version_id IS NULL OR version.allowed_operations @> '["query"]'::jsonb)
          AND COALESCE(audit.after->>'status',CASE WHEN event.updated_at<=bounds.cutoff THEN event.status END)='confirmed'
          AND COALESCE((audit.after->>'deleted')::boolean,
              CASE WHEN event.updated_at<=bounds.cutoff THEN event.deleted END) IS FALSE))
      AND CASE WHEN $12 THEN observation.observed_at<bounds.start ELSE
          CASE WHEN $10='interval' THEN CASE
              WHEN $11 THEN observation.observed_at<bounds.finish
              WHEN $9='increment' THEN COALESCE(observation.effective_start,observation.observed_at)>=bounds.start
                  AND COALESCE(observation.effective_start,observation.observed_at)<bounds.finish
              WHEN $9='interval_total' THEN observation.effective_end IS NOT NULL
                  AND observation.effective_start>=bounds.start AND observation.effective_end<=bounds.finish
              ELSE (observation.effective_end IS NOT NULL AND observation.effective_start<bounds.finish AND observation.effective_end>bounds.start)
                  OR (observation.effective_end IS NULL AND observation.effective_start<observation.observed_at
                      AND observation.effective_start<bounds.finish AND observation.observed_at>bounds.start)
                  OR (observation.effective_end IS NULL AND observation.observed_at>=bounds.predecessor
                      AND observation.observed_at<bounds.finish)
              END
          ELSE observation.observed_at<bounds.finish AND ($11 OR observation.observed_at>=bounds.predecessor) END END
), ranked AS (
    SELECT eligible.*,row_number() OVER(PARTITION BY feature_version,
        CASE WHEN feature_version='pre-event-v1' THEN NULL ELSE id END,
        observed_at,source_calendar_date,sequence,account,device
        ORDER BY ingested_at DESC,fetched_at DESC,id DESC) AS rank FROM eligible
)
SELECT id::text,value AS value_number,value_text,value_boolean,
    (extract(epoch FROM observed_at)*1000000)::bigint AS observed,
    (extract(epoch FROM effective_start)*1000000)::bigint AS effective_start,
    (extract(epoch FROM effective_end)*1000000)::bigint AS effective_end,
    source_ref,(extract(epoch FROM recorded_at)*1000000)::bigint AS recorded,
    (extract(epoch FROM ingested_at)*1000000)::bigint AS ingested,
    overlay(to_jsonb(date_trunc('second',ingested_at))#>>'{}' placing
        CASE WHEN extract(microseconds FROM ingested_at)::bigint%1000000=0 THEN ''
            ELSE '.'||to_char(ingested_at,'US') END from 20 for 0) AS ingested_display,
    sequence,account,device,source_entry_id,projection_version,coverage
FROM ranked CROSS JOIN bounds WHERE ($12 OR rank=1) AND ($12 OR NOT $11 OR observed_at>=bounds.start)
ORDER BY CASE WHEN $12 THEN observed_at END DESC,CASE WHEN $12 THEN ingested_at END DESC,
    CASE WHEN $12 THEN id END DESC,observed_at,recorded_at,ingested_at,sequence,id
LIMIT CASE WHEN $12 THEN 1 ELSE 10001 END
