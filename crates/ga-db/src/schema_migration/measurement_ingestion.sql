ALTER TABLE public.metric_definition_versions ADD COLUMN category_domain JSONB;
ALTER TABLE public.measurements ADD COLUMN ingested_at TIMESTAMPTZ DEFAULT now();

UPDATE public.measurements AS measurement
SET ingested_at = COALESCE(
    (SELECT payload.fetched_at FROM public.source_payloads AS payload
     WHERE payload.id = measurement.source_ref),
    CURRENT_TIMESTAMP
);

ALTER TABLE public.measurements ALTER COLUMN ingested_at SET NOT NULL;
