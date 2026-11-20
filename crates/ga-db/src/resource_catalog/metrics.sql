            ) IS TRUE
        )) AS permitted
    FROM event_definition_versions AS version CROSS JOIN destinations AS destination
    GROUP BY version.id,destination.categories
), selected AS (
    SELECT definition.key,version.*,
        ($5 IS NULL OR definition.namespace='system' OR (
            NOT EXISTS (
                SELECT FROM event_metric_mappings AS mapping
                JOIN metric_definition_versions AS mapped ON mapped.id=mapping.metric_definition_version_id
                JOIN permissions AS facts ON facts.id=mapping.event_definition_version_id AND facts.category='facts'
                WHERE mapped.definition_id=definition.id AND NOT facts.permitted
            )
        )) AND (definition.namespace='system' OR $7 OR NOT EXISTS (
            SELECT FROM metric_observations WHERE metric=definition.key AND feature_version='source-plugin-v1'
        )) AS facts_allowed
    FROM metric_definitions AS definition JOIN metric_definition_versions AS version
        ON version.definition_id=definition.id AND version.version=definition.current_version
    WHERE definition.status='active'
        AND definition.namespace=CASE WHEN $10='system_metrics' THEN 'system' ELSE 'user' END
        AND (definition.namespace='system' OR definition.owner_id=$6)
        AND ($1::text IS NULL OR definition.key>$1)
        AND ($2::text IS NULL OR definition.key=$2)
        AND ($5 IS NULL OR definition.namespace='system' OR (
            EXISTS (SELECT FROM event_metric_mappings AS mapping
                JOIN metric_definition_versions AS mapped ON mapped.id=mapping.metric_definition_version_id
                WHERE mapped.definition_id=definition.id)
            AND NOT EXISTS (
                SELECT FROM event_metric_mappings AS mapping
                JOIN metric_definition_versions AS mapped ON mapped.id=mapping.metric_definition_version_id
                JOIN permissions AS schema ON schema.id=mapping.event_definition_version_id AND schema.category='schema'
                WHERE mapped.definition_id=definition.id AND NOT schema.permitted
            )
        ))
        AND NOT (definition.key=ANY($9::text[]))
    ORDER BY definition.key LIMIT $3+1
), schema_refs AS (
    SELECT DISTINCT mapping.event_definition_version_id AS id
    FROM (SELECT definition_id FROM selected ORDER BY key LIMIT $3) AS sent
    JOIN metric_definition_versions AS version ON version.definition_id=sent.definition_id
    JOIN event_metric_mappings AS mapping ON mapping.metric_definition_version_id=version.id
    WHERE $11::text IS NOT NULL AND $10::text='tracker_metrics'
), rows AS (
    SELECT selected.key,
        jsonb_build_object('id',key,'version',version,'version_id',id,'kind','metric',
            'labels',labels,'semantic_type',value_kind,'unit',unit,'time_semantics',time_semantics,
            'operations',(SELECT COALESCE(jsonb_agg(method ORDER BY method),'[]'::jsonb)
                FROM jsonb_array_elements(allowed_methods) AS methods(method)),
            'availability',CASE WHEN facts_allowed THEN jsonb_build_object(
                'canonical_observations',observations.count,'measurements',measurements.count,
                'latest_at',to_char(GREATEST(observations.latest,measurements.latest) AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS') ||
                    CASE WHEN extract(microseconds FROM GREATEST(observations.latest,measurements.latest))::bigint%1000000=0
                        THEN '' ELSE to_char(GREATEST(observations.latest,measurements.latest) AT TIME ZONE 'UTC','.US') END || '+00:00',
                'quality','valid observations only; completeness is not implied',
                'sources',array_remove(ARRAY[
                    CASE WHEN observations.count>0 THEN 'canonical_observations' END,
                    CASE WHEN measurements.count>0 THEN 'measurements' END
                ],NULL)
            ) END
        ) || CASE WHEN $4 THEN jsonb_build_object(
            'dimension',dimension,'scale_id',scale_id,'scale_version',scale_version,
            'aggregation',aggregation,'coverage_policy',coverage_policy,
            'minimum',minimum,'maximum',maximum,'category_domain',category_domain,'schema_hash',schema_hash
        ) ELSE '{}'::jsonb END AS document
    FROM selected
    CROSS JOIN LATERAL (SELECT count(*),max(COALESCE(observed_at,effective_start)) AS latest
        FROM metric_observations WHERE metric_definition_version_id=selected.id AND valid
            AND facts_allowed) AS observations
    CROSS JOIN LATERAL (SELECT count(*),max(ts) AS latest FROM measurements
        WHERE metric_definition_version_id=selected.id AND quality='observed'
            AND facts_allowed) AS measurements
