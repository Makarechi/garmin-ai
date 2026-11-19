WITH bounds AS (SELECT 'epoch'::timestamptz+$2::interval AS start,
    'epoch'::timestamptz+$3::interval AS finish), cutoff AS (SELECT 'epoch'::timestamptz+$4::interval AS at), observations AS MATERIALIZED (
    SELECT observation.*,event.updated_at AS event_updated_at,
        prior.id AS audit_id,prior.after AS audit_state,
        CASE WHEN event.updated_at<=cutoff.at THEN jsonb_build_object(
            'source',event.source,'status',event.status,'validation_status',event.validation_status,
            'assertion_kind',event.assertion_kind,'topology',event.topology) END AS event_state
    FROM metric_observations AS observation CROSS JOIN cutoff CROSS JOIN bounds
    LEFT JOIN events AS event ON event.id=observation.source_entry_id
    LEFT JOIN event_definition_versions AS event_version ON event_version.id=event.definition_version_id
    LEFT JOIN LATERAL (SELECT id,after FROM audit_log
        WHERE event_id=event.id AND created_at<=cutoff.at ORDER BY created_at DESC,id DESC LIMIT 1) AS prior ON true
    WHERE observation.metric_definition_version_id=$1
      AND observation.observed_at>=bounds.start AND observation.observed_at<bounds.finish
      AND observation.ingested_at<=cutoff.at AND observation.quality='observed'
      AND (observation.valid OR observation.invalidated_at>cutoff.at)
      AND (observation.source_entry_id IS NULL OR (
          (event.definition_version_id IS NULL OR event_version.allowed_operations @> '["query"]'::jsonb)
          AND COALESCE(prior.after->>'status',CASE WHEN event.updated_at<=cutoff.at THEN event.status END)='confirmed'
          AND COALESCE((prior.after->>'deleted')::boolean,
              CASE WHEN event.updated_at<=cutoff.at THEN event.deleted END) IS FALSE))
    ORDER BY observation.observed_at,observation.id LIMIT $5::bigint+1

) SELECT source_entry_id,account,device,
    audit_id IS NOT NULL AND (audit_state IS NULL OR jsonb_typeof(audit_state)<>'object') AS invalid_snapshot
FROM observations ORDER BY observed_at,id
