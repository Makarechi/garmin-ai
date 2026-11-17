UPDATE events
SET start = fields.start, "end" = fields."end", topology = fields.topology, timezone = fields.timezone,
    source = fields.source, status = fields.status, confidence = fields.confidence,
    original_text = fields.original_text, payload = fields.payload, recorded_at = fields.recorded_at,
    ingested_at = fields.recorded_at, updated_at = fields.recorded_at, time_precision = fields.time_precision,
    assertion_kind = fields.assertion_kind, producer = fields.producer, transport = fields.transport,
    author = fields.author, evidence_refs = fields.evidence_refs, validation_status = fields.validation_status,
    clock_uncertainty_seconds = NULL, envelope_version = 1, revision = revision + 1
FROM jsonb_to_record($2::jsonb) AS fields (
    start timestamptz, "end" timestamptz, topology text, timezone text, source text, status text,
    confidence double precision, original_text text, payload jsonb, recorded_at timestamptz,
    time_precision text, assertion_kind text, producer text, transport text, author text,
    evidence_refs jsonb, validation_status text
)
WHERE id = $1
