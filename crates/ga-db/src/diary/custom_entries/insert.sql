INSERT INTO events (
    id, definition_version_id, kind, start, "end", topology, timezone, source, status,
    confidence, original_text, payload, recorded_at, ingested_at, time_precision,
    assertion_kind, producer, transport, author, evidence_refs, validation_status,
    envelope_version, revision, deleted, idempotency_key
)
SELECT $1, $2, fields.kind, fields.start, fields."end", fields.topology, fields.timezone,
    fields.source, fields.status, fields.confidence, fields.original_text, fields.payload,
    fields.recorded_at, fields.recorded_at, fields.time_precision, fields.assertion_kind,
    fields.producer, fields.transport, fields.author, fields.evidence_refs, fields.validation_status,
    1, 1, false, $4
FROM jsonb_to_record($3::jsonb) AS fields (
    kind text, start timestamptz, "end" timestamptz, topology text, timezone text,
    source text, status text, confidence double precision, original_text text, payload jsonb,
    recorded_at timestamptz, time_precision text, assertion_kind text, producer text,
    transport text, author text, evidence_refs jsonb, validation_status text
)
