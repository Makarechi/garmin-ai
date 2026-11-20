"""Summarize equal stored projections for synthetic Garmin history workloads."""

import hashlib
import json
import os

import psycopg
from psycopg.conninfo import conninfo_to_dict

CHECKS = {
    "raw": """
        SELECT jsonb_build_array(
            source,endpoint,source_key,payload_hash,archive_key,parser_version,status,
            source_updated_at,payload
        )::text
        FROM source_payloads ORDER BY source,endpoint,source_key,payload_hash
    """,
    "daily": """
        SELECT ((to_jsonb(h)-'updated_at'-'sources') || jsonb_build_object('sources',
            (SELECT jsonb_object_agg(
                CASE WHEN starts_with(s.key,'payload:') THEN
                    'payload:' || concat(raw.endpoint,':',raw.source_key,':',raw.payload_hash)
                    ELSE s.key END,
                CASE WHEN starts_with(s.key,'time:') THEN
                    to_jsonb((s.value #>> '{}')::timestamptz IS NOT NULL)
                    WHEN starts_with(s.key,'payload:') AND raw.id IS NOT NULL THEN
                    jsonb_build_array(s.value,raw.source,raw.endpoint,raw.source_key,raw.payload_hash)
                    WHEN raw.id IS NOT NULL THEN
                    jsonb_build_array(raw.source,raw.endpoint,raw.source_key,raw.payload_hash)
                    ELSE s.value END)
            FROM jsonb_each(h.sources) s
                LEFT JOIN source_payloads raw
                ON raw.id = CASE
                    WHEN starts_with(s.key,'field:') THEN (s.value #>> '{}')::uuid
                    WHEN starts_with(s.key,'payload:') THEN substring(s.key FROM 9)::uuid
                    END
            )))::text
        FROM health_days h ORDER BY day
    """,
    "samples": """
        SELECT jsonb_build_array(
            m.ts,m.metric,m.source,m.local_date,m.value,m.unit,m.quality,m.details,
            definition.key,contract.version,contract.schema_hash,raw.endpoint,raw.source_key,
            raw.payload_hash
        )::text
        FROM measurements m
            LEFT JOIN source_payloads raw ON raw.id=m.source_ref
            LEFT JOIN metric_definition_versions contract ON contract.id=m.metric_definition_version_id
            LEFT JOIN metric_definitions definition ON definition.id=contract.definition_id
        ORDER BY m.ts,m.metric,m.source
    """,
    "revisions": """
        SELECT jsonb_build_array(
            m.ts,m.metric,m.source,m.local_date,m.value,m.unit,m.quality,m.details,m.deleted,
            definition.key,contract.version,contract.schema_hash,raw.endpoint,raw.source_key,
            raw.payload_hash
        )::text
        FROM measurement_revisions m
            LEFT JOIN source_payloads raw ON raw.id=m.source_ref
            LEFT JOIN metric_definition_versions contract ON contract.id=m.metric_definition_version_id
            LEFT JOIN metric_definitions definition ON definition.id=contract.definition_id
        ORDER BY m.ts,m.metric,m.source,raw.payload_hash,m.deleted
    """,
    "jobs": """
        SELECT jsonb_build_array(kind,dedup_key,payload->>'endpoint',payload->>'key',status,attempts,
            last_error,lease_token IS NULL,lease_until IS NULL,completed_at IS NOT NULL)::text
        FROM jobs WHERE kind='garmin_endpoint' AND starts_with(dedup_key,'syncwindow:')
        ORDER BY dedup_key
    """,
}


def snapshot(url):
    dsn = url.replace("postgresql+psycopg://", "postgresql://", 1)
    name = conninfo_to_dict(dsn).get("dbname", "")
    if not name.endswith("_test"):
        raise ValueError("A dedicated synthetic history database is required")
    result = {}
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as guard:
            guard.execute("SET TRANSACTION READ ONLY")
            guard.execute("SET LOCAL TIME ZONE 'UTC'")
            if guard.execute("SELECT current_database()").fetchone() != (name,):
                raise ValueError("Synthetic history database does not match the configured target")
        for label, sql in CHECKS.items():
            digest = hashlib.sha256()
            count = 0
            with connection.cursor(name=f"history_{label}") as cursor:
                cursor.itersize = 200
                cursor.execute(sql)
                for (line,) in cursor:
                    digest.update((line + "\n").encode())
                    count += 1
            result[label] = {"rows": count, "sha256": digest.hexdigest()}
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot(os.environ["GA_DATABASE_URL"])))
