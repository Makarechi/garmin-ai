CREATE TABLE public.measurement_revisions (
    id uuid NOT NULL,
    ts timestamp with time zone NOT NULL,
    metric character varying NOT NULL,
    source character varying NOT NULL,
    local_date date NOT NULL,
    value double precision NOT NULL,
    unit character varying NOT NULL,
    metric_definition_version_id uuid,
    source_ref uuid,
    quality character varying DEFAULT 'observed'::character varying NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    ingested_at timestamp with time zone NOT NULL,
    deleted boolean DEFAULT false NOT NULL,
    CONSTRAINT measurement_revisions_pkey PRIMARY KEY (id),
    CONSTRAINT uq_measurement_revision_source UNIQUE
        (ts, metric, source, source_ref, ingested_at, deleted),
    CONSTRAINT measurement_revisions_metric_definition_version_id_fkey
        FOREIGN KEY (metric_definition_version_id)
        REFERENCES public.metric_definition_versions(id) ON DELETE RESTRICT
);

CREATE INDEX ix_measurement_revisions_asof ON public.measurement_revisions
    (metric_definition_version_id, ts, ingested_at);
CREATE INDEX ix_measurement_revisions_local_date ON public.measurement_revisions (local_date);
CREATE INDEX ix_measurement_revisions_metric ON public.measurement_revisions (metric);
CREATE INDEX ix_measurement_revisions_metric_definition_version_id
    ON public.measurement_revisions (metric_definition_version_id);
CREATE INDEX ix_measurement_revisions_source_ref ON public.measurement_revisions (source_ref);
CREATE INDEX ix_measurement_revisions_ts ON public.measurement_revisions (ts);

INSERT INTO public.measurement_revisions (
    id, ts, metric, source, local_date, value, unit,
    metric_definition_version_id, source_ref, quality, details, ingested_at
)
SELECT gen_random_uuid(), ts, metric, source, local_date, value, unit,
       metric_definition_version_id, source_ref, quality, details, ingested_at
FROM public.measurements;
