-- Current public schema recorded from pinned Alembic head c54f8a72d130.
-- Converted to a Timescale hypertable before the migration is recorded.

CREATE FUNCTION public.reject_event_definition_version_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'event definition versions are immutable';
        END;
        $$;

CREATE FUNCTION public.reject_metric_definition_version_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            RAISE EXCEPTION 'metric definition versions are immutable';
        END;
        $$;

CREATE TABLE public.activities (
    id character varying NOT NULL,
    kind character varying NOT NULL,
    name character varying,
    start timestamp with time zone NOT NULL,
    "end" timestamp with time zone NOT NULL,
    timezone character varying NOT NULL,
    duration_seconds double precision,
    moving_seconds double precision,
    distance_m double precision,
    avg_hr double precision,
    max_hr double precision,
    avg_speed_mps double precision,
    calories double precision,
    cadence double precision,
    ascent_m double precision,
    descent_m double precision,
    aerobic_effect double precision,
    anaerobic_effect double precision,
    training_load double precision,
    fit_key character varying,
    details jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT activities_check CHECK (("end" >= start))
);

CREATE TABLE public.activity_parts (
    activity_id character varying NOT NULL,
    kind character varying NOT NULL,
    sequence integer NOT NULL,
    payload jsonb NOT NULL
);

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);

CREATE TABLE public.app_state (
    key character varying NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    event_id uuid NOT NULL,
    action character varying NOT NULL,
    before jsonb,
    after jsonb,
    actor character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    operation_id uuid
);

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;

CREATE TABLE public.channel_bindings (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    channel character varying NOT NULL,
    channel_instance_id character varying NOT NULL,
    external_id character varying NOT NULL,
    confirmation_method character varying NOT NULL,
    confirmed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.conversations (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    channel character varying NOT NULL,
    channel_instance_id character varying NOT NULL,
    external_conversation_id character varying,
    memory_epoch uuid NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    share_owner_memory boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.event_definition_versions (
    id uuid NOT NULL,
    definition_id uuid NOT NULL,
    version integer NOT NULL,
    schema jsonb NOT NULL,
    schema_hash character varying NOT NULL,
    topology character varying NOT NULL,
    field_metadata jsonb NOT NULL,
    labels jsonb NOT NULL,
    privacy character varying NOT NULL,
    allowed_operations jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_definition_versions_privacy CHECK ((privacy IN ('private', 'sensitive'))),
    CONSTRAINT ck_event_definition_versions_topology CHECK ((topology IN ('point', 'open_interval', 'bounded_interval', 'flexible'))),
    CONSTRAINT ck_event_definition_versions_version CHECK ((version >= 1))
);

CREATE TABLE public.event_definitions (
    id uuid NOT NULL,
    owner_id uuid,
    namespace character varying NOT NULL,
    key character varying NOT NULL,
    status character varying NOT NULL,
    revision integer NOT NULL,
    current_version integer,
    draft jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_definitions_namespace_owner CHECK (((((namespace)::text = 'system'::text) AND (owner_id IS NULL)) OR (((namespace)::text = 'user'::text) AND (owner_id IS NOT NULL)))),
    CONSTRAINT ck_event_definitions_revision CHECK ((revision >= 1)),
    CONSTRAINT ck_event_definitions_status CHECK ((status IN ('draft', 'proposed', 'active', 'retired')))
);

CREATE TABLE public.event_metric_mappings (
    id uuid NOT NULL,
    event_definition_version_id uuid NOT NULL,
    field_id character varying NOT NULL,
    metric_definition_version_id uuid NOT NULL,
    projection_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_event_metric_mapping_version CHECK ((projection_version >= 1))
);

CREATE TABLE public.events (
    id uuid NOT NULL,
    kind character varying NOT NULL,
    start timestamp with time zone NOT NULL,
    "end" timestamp with time zone,
    timezone character varying NOT NULL,
    source character varying NOT NULL,
    confidence double precision NOT NULL,
    status character varying NOT NULL,
    original_text text,
    payload jsonb NOT NULL,
    revision integer NOT NULL,
    deleted boolean NOT NULL,
    idempotency_key character varying,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    definition_version_id uuid,
    topology character varying DEFAULT 'point'::character varying NOT NULL,
    envelope_version integer DEFAULT 1 NOT NULL,
    time_precision character varying DEFAULT 'instant'::character varying NOT NULL,
    assertion_kind character varying DEFAULT 'user_report'::character varying NOT NULL,
    producer character varying DEFAULT 'owner'::character varying NOT NULL,
    transport character varying,
    author character varying,
    evidence_refs jsonb DEFAULT '[]'::jsonb NOT NULL,
    validation_status character varying DEFAULT 'schema_validated'::character varying NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    clock_uncertainty_seconds integer,
    CONSTRAINT ck_events_assertion_kind CHECK ((assertion_kind IN ('user_report', 'device_measurement', 'derived', 'inferred'))),
    CONSTRAINT ck_events_clock_uncertainty CHECK (((clock_uncertainty_seconds IS NULL) OR ((clock_uncertainty_seconds >= 0) AND (clock_uncertainty_seconds <= 31536000)))),
    CONSTRAINT ck_events_envelope_version CHECK ((envelope_version = 1)),
    CONSTRAINT ck_events_evidence_refs_array CHECK ((jsonb_typeof(evidence_refs) = 'array'::text)),
    CONSTRAINT ck_events_time_precision CHECK ((time_precision IN ('instant', 'interval', 'calendar_date', 'unknown'))),
    CONSTRAINT ck_events_topology CHECK ((topology IN ('point', 'open_interval', 'bounded_interval', 'flexible'))),
    CONSTRAINT ck_events_validation_status CHECK ((validation_status IN ('trusted', 'schema_validated', 'needs_confirmation'))),
    CONSTRAINT events_check CHECK ((("end" IS NULL) OR ("end" >= start))),
    CONSTRAINT events_confidence_check CHECK (((confidence >= (0)::double precision) AND (confidence <= (1)::double precision)))
);

CREATE TABLE public.health_days (
    day date NOT NULL,
    sleep_score double precision,
    sleep_seconds double precision,
    deep_seconds double precision,
    rem_seconds double precision,
    light_seconds double precision,
    awake_seconds double precision,
    hrv_nightly_avg double precision,
    hrv_weekly_avg double precision,
    hrv_baseline_low double precision,
    hrv_baseline_high double precision,
    hrv_status character varying,
    resting_hr double precision,
    body_battery_high double precision,
    body_battery_low double precision,
    body_battery_charged double precision,
    body_battery_drained double precision,
    stress_avg double precision,
    stress_max double precision,
    steps integer,
    active_calories double precision,
    intensity_minutes double precision,
    training_readiness_score double precision,
    recovery_time_minutes double precision,
    training_status character varying,
    vo2max double precision,
    sources jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    hydration_ml double precision
);

CREATE TABLE public.inbound_messages (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    channel character varying NOT NULL,
    channel_instance_id character varying NOT NULL,
    external_event_id character varying NOT NULL,
    external_message_id character varying,
    sender_ref character varying NOT NULL,
    occurred_at timestamp with time zone,
    received_at timestamp with time zone NOT NULL,
    kind character varying NOT NULL,
    normalized_text text,
    envelope jsonb NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    status character varying DEFAULT 'pending'::character varying NOT NULL,
    operation_id uuid NOT NULL,
    supersedes_id uuid,
    legacy_telegram_update_id bigint,
    CONSTRAINT ck_inbound_revision_positive CHECK ((revision >= 1))
);

CREATE TABLE public.insights (
    id uuid NOT NULL,
    category character varying NOT NULL,
    statement character varying NOT NULL,
    evidence jsonb NOT NULL,
    sample_size integer NOT NULL,
    effect_size double precision,
    status character varying NOT NULL,
    dedup_key character varying NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.jobs (
    id uuid NOT NULL,
    kind character varying NOT NULL,
    payload jsonb NOT NULL,
    dedup_key character varying NOT NULL,
    status character varying NOT NULL,
    run_at timestamp with time zone NOT NULL,
    lease_until timestamp with time zone,
    lease_token uuid,
    attempts integer NOT NULL,
    last_error character varying,
    completed_at timestamp with time zone
);

CREATE TABLE public.measurement_history (
    id uuid NOT NULL,
    ts timestamp with time zone NOT NULL,
    metric character varying NOT NULL,
    source character varying NOT NULL,
    value double precision NOT NULL,
    metric_definition_version_id uuid,
    source_ref uuid NOT NULL,
    quality character varying NOT NULL,
    known_at timestamp with time zone NOT NULL,
    superseded_at timestamp with time zone NOT NULL
);

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
    deleted boolean DEFAULT false NOT NULL
);

CREATE TABLE public.measurements (
    ts timestamp with time zone NOT NULL,
    metric character varying NOT NULL,
    source character varying NOT NULL,
    local_date date NOT NULL,
    value double precision NOT NULL,
    unit character varying NOT NULL,
    source_ref uuid,
    quality character varying NOT NULL,
    details jsonb NOT NULL,
    metric_definition_version_id uuid,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.message_delivery_receipts (
    id uuid NOT NULL,
    outbox_message_id uuid NOT NULL,
    state character varying NOT NULL,
    observed_at timestamp with time zone NOT NULL,
    provider_reference character varying,
    detail text
);

CREATE TABLE public.metric_definition_versions (
    id uuid NOT NULL,
    definition_id uuid NOT NULL,
    version integer NOT NULL,
    value_kind character varying NOT NULL,
    unit character varying,
    dimension character varying NOT NULL,
    scale_id character varying,
    scale_version integer,
    aggregation character varying NOT NULL,
    coverage_policy jsonb NOT NULL,
    time_semantics character varying NOT NULL,
    minimum double precision,
    maximum double precision,
    labels jsonb NOT NULL,
    allowed_methods jsonb NOT NULL,
    schema_hash character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    category_domain jsonb,
    CONSTRAINT ck_metric_definition_versions_kind CHECK ((value_kind IN ('physical_number', 'increment', 'interval_total', 'cumulative_counter', 'ordinal', 'nominal', 'boolean'))),
    CONSTRAINT ck_metric_definition_versions_time CHECK ((time_semantics IN ('point', 'interval', 'calendar_period'))),
    CONSTRAINT ck_metric_definition_versions_version CHECK ((version >= 1))
);

CREATE TABLE public.metric_definitions (
    id uuid NOT NULL,
    owner_id uuid,
    namespace character varying NOT NULL,
    key character varying NOT NULL,
    status character varying NOT NULL,
    current_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_metric_definitions_namespace_owner CHECK (((((namespace)::text = 'system'::text) AND (owner_id IS NULL)) OR (((namespace)::text = 'user'::text) AND (owner_id IS NOT NULL)))),
    CONSTRAINT ck_metric_definitions_status CHECK ((status IN ('active', 'retired')))
);

CREATE TABLE public.metric_observations (
    id uuid NOT NULL,
    metric character varying NOT NULL,
    value double precision,
    unit character varying NOT NULL,
    observed_at timestamp with time zone,
    effective_start timestamp with time zone,
    source_calendar_date date NOT NULL,
    source_ref uuid NOT NULL,
    fetched_at timestamp with time zone NOT NULL,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    timezone character varying NOT NULL,
    account character varying,
    device character varying,
    quality character varying NOT NULL,
    sequence integer NOT NULL,
    feature_version character varying NOT NULL,
    value_text character varying,
    value_boolean boolean,
    metric_definition_version_id uuid,
    source_entry_id uuid,
    field_id character varying,
    projection_version integer,
    effective_end timestamp with time zone,
    recorded_at timestamp with time zone,
    uploaded_at timestamp with time zone,
    "precision" double precision,
    coverage double precision,
    valid boolean DEFAULT true NOT NULL,
    invalidated_at timestamp with time zone,
    CONSTRAINT ck_metric_observations_coverage CHECK (((coverage IS NULL) OR ((coverage >= (0)::double precision) AND (coverage <= (1)::double precision)))),
    CONSTRAINT ck_metric_observations_typed_value CHECK ((((
CASE
    WHEN (value IS NULL) THEN 0
    ELSE 1
END +
CASE
    WHEN (value_text IS NULL) THEN 0
    ELSE 1
END) +
CASE
    WHEN (value_boolean IS NULL) THEN 0
    ELSE 1
END) = 1))
);

CREATE TABLE public.module_configs (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    pack_key character varying NOT NULL,
    tracking_enabled boolean NOT NULL,
    collection_enabled boolean NOT NULL,
    reminders_enabled boolean NOT NULL,
    visible boolean NOT NULL,
    llm_enabled boolean NOT NULL,
    outcome_goal text,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    revision integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_module_configs_revision CHECK ((revision >= 1))
);

CREATE TABLE public.outbox_messages (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    conversation_id uuid NOT NULL,
    inbound_message_id uuid,
    operation_id uuid NOT NULL,
    intent jsonb NOT NULL,
    dedup_key character varying NOT NULL,
    state character varying DEFAULT 'queued'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    provider_reference character varying,
    legacy_key character varying,
    next_attempt_at timestamp with time zone,
    lease_until timestamp with time zone,
    lease_token uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    memory_fence jsonb
);

CREATE TABLE public.pending_questions (
    id uuid NOT NULL,
    kind character varying NOT NULL,
    event_id uuid,
    text character varying NOT NULL,
    evidence jsonb NOT NULL,
    priority double precision NOT NULL,
    earliest_send_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    sent_at timestamp with time zone,
    status character varying NOT NULL,
    dedup_key character varying NOT NULL,
    attempts integer NOT NULL
);

CREATE TABLE public.people (
    id uuid NOT NULL,
    singleton boolean DEFAULT true NOT NULL,
    locale character varying NOT NULL,
    timezone character varying NOT NULL,
    units character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_people_single_owner CHECK (singleton),
    CONSTRAINT ck_people_units CHECK ((units IN ('metric', 'imperial')))
);

CREATE TABLE public.source_connections (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    provider character varying NOT NULL,
    namespace character varying NOT NULL,
    external_id character varying NOT NULL,
    confirmation_method character varying NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    confirmed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.source_payloads (
    id uuid NOT NULL,
    source character varying NOT NULL,
    endpoint character varying NOT NULL,
    source_key character varying NOT NULL,
    payload_hash character varying NOT NULL,
    payload jsonb,
    archive_key character varying NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    source_updated_at timestamp with time zone,
    parser_version integer NOT NULL,
    status character varying NOT NULL
);

CREATE TABLE public.telegram_updates (
    id bigint NOT NULL,
    payload jsonb NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    status character varying NOT NULL
);

CREATE SEQUENCE public.telegram_updates_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.telegram_updates_id_seq OWNED BY public.telegram_updates.id;

CREATE TABLE public.timeline_intervals (
    id character varying NOT NULL,
    start timestamp with time zone NOT NULL,
    "end" timestamp with time zone NOT NULL,
    label character varying NOT NULL,
    source character varying NOT NULL,
    confidence double precision NOT NULL,
    confirmed boolean NOT NULL,
    evidence jsonb NOT NULL
);

CREATE TABLE public.tracker_configs (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    definition_id uuid NOT NULL,
    shortcut character varying,
    reminder_enabled boolean NOT NULL,
    reminder_time character varying,
    reminder_timezone character varying,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    revision integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_tracker_configs_reminder_time CHECK (((reminder_time IS NULL) OR ((reminder_time)::text ~ '^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'::text))),
    CONSTRAINT ck_tracker_configs_revision CHECK ((revision >= 1))
);

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);

ALTER TABLE ONLY public.telegram_updates ALTER COLUMN id SET DEFAULT nextval('public.telegram_updates_id_seq'::regclass);

ALTER TABLE ONLY public.activities
    ADD CONSTRAINT activities_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.activity_parts
    ADD CONSTRAINT activity_parts_pkey PRIMARY KEY (activity_id, kind, sequence);

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);

ALTER TABLE ONLY public.app_state
    ADD CONSTRAINT app_state_pkey PRIMARY KEY (key);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT channel_bindings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_definition_versions
    ADD CONSTRAINT event_definition_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_definitions
    ADD CONSTRAINT event_definitions_key_key UNIQUE (key);

ALTER TABLE ONLY public.event_definitions
    ADD CONSTRAINT event_definitions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_metric_mappings
    ADD CONSTRAINT event_metric_mappings_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_idempotency_key_key UNIQUE (idempotency_key);

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.health_days
    ADD CONSTRAINT health_days_pkey PRIMARY KEY (day);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_legacy_telegram_update_id_key UNIQUE (legacy_telegram_update_id);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.insights
    ADD CONSTRAINT insights_dedup_key_key UNIQUE (dedup_key);

ALTER TABLE ONLY public.insights
    ADD CONSTRAINT insights_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_dedup_key_key UNIQUE (dedup_key);

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.measurement_history
    ADD CONSTRAINT measurement_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.measurement_revisions
    ADD CONSTRAINT measurement_revisions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.measurements
    ADD CONSTRAINT measurements_pkey PRIMARY KEY (ts, metric, source);

ALTER TABLE ONLY public.message_delivery_receipts
    ADD CONSTRAINT message_delivery_receipts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.metric_definition_versions
    ADD CONSTRAINT metric_definition_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_key_key UNIQUE (key);

ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.metric_observations
    ADD CONSTRAINT metric_observations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.metric_observations
    ADD CONSTRAINT metric_observations_source_ref_fetched_at_metric_sequence_f_key UNIQUE (source_ref, fetched_at, metric, sequence, feature_version);

ALTER TABLE ONLY public.module_configs
    ADD CONSTRAINT module_configs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_dedup_key_key UNIQUE (dedup_key);

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_legacy_key_key UNIQUE (legacy_key);

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.pending_questions
    ADD CONSTRAINT pending_questions_dedup_key_key UNIQUE (dedup_key);

ALTER TABLE ONLY public.pending_questions
    ADD CONSTRAINT pending_questions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.people
    ADD CONSTRAINT people_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_connections
    ADD CONSTRAINT source_connections_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_payloads
    ADD CONSTRAINT source_payloads_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.source_payloads
    ADD CONSTRAINT source_payloads_source_endpoint_source_key_payload_hash_key UNIQUE (source, endpoint, source_key, payload_hash);

ALTER TABLE ONLY public.telegram_updates
    ADD CONSTRAINT telegram_updates_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.timeline_intervals
    ADD CONSTRAINT timeline_intervals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tracker_configs
    ADD CONSTRAINT tracker_configs_definition_id_key UNIQUE (definition_id);

ALTER TABLE ONLY public.tracker_configs
    ADD CONSTRAINT tracker_configs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT uq_channel_external_identity UNIQUE (channel, channel_instance_id, external_id);

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT uq_conversation_external_identity UNIQUE (owner_id, channel, channel_instance_id, external_conversation_id);

ALTER TABLE ONLY public.message_delivery_receipts
    ADD CONSTRAINT uq_delivery_receipt_evidence UNIQUE (outbox_message_id, state, observed_at);

ALTER TABLE ONLY public.event_definition_versions
    ADD CONSTRAINT uq_event_definition_version UNIQUE (definition_id, version);

ALTER TABLE ONLY public.event_metric_mappings
    ADD CONSTRAINT uq_event_metric_mapping_projection UNIQUE (event_definition_version_id, field_id, projection_version);

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT uq_inbound_transport_revision UNIQUE (channel, channel_instance_id, external_event_id, revision);

ALTER TABLE ONLY public.measurement_revisions
    ADD CONSTRAINT uq_measurement_revision_source UNIQUE (ts, metric, source, source_ref, ingested_at, deleted);

ALTER TABLE ONLY public.metric_definition_versions
    ADD CONSTRAINT uq_metric_definition_version UNIQUE (definition_id, version);

ALTER TABLE ONLY public.metric_observations
    ADD CONSTRAINT uq_metric_projection_fact UNIQUE (source_entry_id, field_id, projection_version);

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT uq_owner_channel_instance UNIQUE (owner_id, channel, channel_instance_id);

ALTER TABLE ONLY public.module_configs
    ADD CONSTRAINT uq_owner_module_pack UNIQUE (owner_id, pack_key);

ALTER TABLE ONLY public.source_connections
    ADD CONSTRAINT uq_owner_source_namespace UNIQUE (owner_id, provider, namespace);

ALTER TABLE ONLY public.people
    ADD CONSTRAINT uq_people_singleton UNIQUE (singleton);

ALTER TABLE ONLY public.source_connections
    ADD CONSTRAINT uq_source_external_identity UNIQUE (provider, namespace, external_id);

CREATE INDEX ix_activities_start ON public.activities USING btree (start);

CREATE INDEX ix_audit_log_event_id ON public.audit_log USING btree (event_id);

CREATE INDEX ix_audit_log_operation_id ON public.audit_log USING btree (operation_id);

CREATE INDEX ix_channel_bindings_owner_id ON public.channel_bindings USING btree (owner_id);

CREATE INDEX ix_conversations_owner_id ON public.conversations USING btree (owner_id);

CREATE INDEX ix_event_definition_versions_definition_id ON public.event_definition_versions USING btree (definition_id);

CREATE INDEX ix_event_definitions_owner_id ON public.event_definitions USING btree (owner_id);

CREATE INDEX ix_event_metric_mappings_event_definition_version_id ON public.event_metric_mappings USING btree (event_definition_version_id);

CREATE INDEX ix_event_metric_mappings_metric_definition_version_id ON public.event_metric_mappings USING btree (metric_definition_version_id);

CREATE INDEX ix_events_definition_version_id ON public.events USING btree (definition_version_id);

CREATE INDEX ix_events_kind ON public.events USING btree (kind);

CREATE INDEX ix_events_start ON public.events USING btree (start);

CREATE INDEX ix_inbound_messages_conversation_id ON public.inbound_messages USING btree (conversation_id);

CREATE INDEX ix_inbound_messages_operation_id ON public.inbound_messages USING btree (operation_id);

CREATE INDEX ix_inbound_messages_owner_id ON public.inbound_messages USING btree (owner_id);

CREATE INDEX ix_inbound_messages_status ON public.inbound_messages USING btree (status);

CREATE INDEX ix_inbound_retention_age ON public.inbound_messages USING btree (received_at, id) WHERE ((status IN ('processed', 'invalid')) AND ((envelope ->> '_text_redacted'::text) IS DISTINCT FROM 'true'::text));

CREATE INDEX ix_job_due ON public.jobs USING btree (status, run_at);

CREATE INDEX ix_jobs_run_at ON public.jobs USING btree (run_at);

CREATE INDEX ix_jobs_status ON public.jobs USING btree (status);

CREATE INDEX ix_measurement_history_asof ON public.measurement_history USING btree (metric_definition_version_id, ts, known_at);

CREATE INDEX ix_measurement_history_metric_definition_version_id ON public.measurement_history USING btree (metric_definition_version_id);

CREATE INDEX ix_measurement_revisions_asof ON public.measurement_revisions USING btree (metric_definition_version_id, ts, ingested_at);

CREATE INDEX ix_measurement_revisions_local_date ON public.measurement_revisions USING btree (local_date);

CREATE INDEX ix_measurement_revisions_metric ON public.measurement_revisions USING btree (metric);

CREATE INDEX ix_measurement_revisions_metric_definition_version_id ON public.measurement_revisions USING btree (metric_definition_version_id);

CREATE INDEX ix_measurement_revisions_source_ref ON public.measurement_revisions USING btree (source_ref);

CREATE INDEX ix_measurement_revisions_ts ON public.measurement_revisions USING btree (ts);

CREATE INDEX ix_measurements_local_date ON public.measurements USING btree (local_date);

CREATE INDEX ix_measurements_metric_definition_version_id ON public.measurements USING btree (metric_definition_version_id);

CREATE INDEX ix_measurements_metric_quality_ts ON public.measurements USING btree (metric, quality, ts);

CREATE INDEX ix_measurements_source_ref ON public.measurements USING btree (source_ref);

CREATE INDEX ix_message_delivery_receipts_outbox_message_id ON public.message_delivery_receipts USING btree (outbox_message_id);

CREATE INDEX ix_metric_definition_versions_definition_id ON public.metric_definition_versions USING btree (definition_id);

CREATE INDEX ix_metric_definitions_owner_id ON public.metric_definitions USING btree (owner_id);

CREATE INDEX ix_metric_observations_asof ON public.metric_observations USING btree (metric, observed_at, ingested_at);

CREATE INDEX ix_metric_observations_metric_definition_version_id ON public.metric_observations USING btree (metric_definition_version_id);

CREATE INDEX ix_metric_observations_source_entry_id ON public.metric_observations USING btree (source_entry_id);

CREATE INDEX ix_module_configs_owner_id ON public.module_configs USING btree (owner_id);

CREATE INDEX ix_outbox_messages_conversation_id ON public.outbox_messages USING btree (conversation_id);

CREATE INDEX ix_outbox_messages_inbound_message_id ON public.outbox_messages USING btree (inbound_message_id);

CREATE INDEX ix_outbox_messages_operation_id ON public.outbox_messages USING btree (operation_id);

CREATE INDEX ix_outbox_messages_owner_id ON public.outbox_messages USING btree (owner_id);

CREATE INDEX ix_outbox_messages_state ON public.outbox_messages USING btree (state);

CREATE INDEX ix_question_answer_retention ON public.pending_questions USING btree (id) WHERE ((evidence ->> 'answer_text'::text) IS NOT NULL);

CREATE INDEX ix_source_connections_owner_id ON public.source_connections USING btree (owner_id);

CREATE INDEX ix_telegram_retention_age ON public.telegram_updates USING btree (received_at, id) WHERE ((status IN ('processed', 'invalid')) AND ((payload ->> '_text_redacted'::text) IS DISTINCT FROM 'true'::text));

CREATE INDEX ix_timeline_intervals_start ON public.timeline_intervals USING btree (start);

CREATE INDEX ix_tracker_configs_definition_id ON public.tracker_configs USING btree (definition_id);

CREATE INDEX ix_tracker_configs_owner_id ON public.tracker_configs USING btree (owner_id);

CREATE INDEX measurements_ts_idx ON public.measurements USING btree (ts DESC);

CREATE TRIGGER event_definition_versions_immutable BEFORE UPDATE ON public.event_definition_versions FOR EACH ROW EXECUTE FUNCTION public.reject_event_definition_version_update();

CREATE TRIGGER metric_definition_versions_immutable BEFORE UPDATE ON public.metric_definition_versions FOR EACH ROW EXECUTE FUNCTION public.reject_metric_definition_version_update();

ALTER TABLE ONLY public.activity_parts
    ADD CONSTRAINT activity_parts_activity_id_fkey FOREIGN KEY (activity_id) REFERENCES public.activities(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.channel_bindings
    ADD CONSTRAINT channel_bindings_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.conversations
    ADD CONSTRAINT conversations_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.event_definition_versions
    ADD CONSTRAINT event_definition_versions_definition_id_fkey FOREIGN KEY (definition_id) REFERENCES public.event_definitions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.event_definitions
    ADD CONSTRAINT event_definitions_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.event_metric_mappings
    ADD CONSTRAINT event_metric_mappings_event_definition_version_id_fkey FOREIGN KEY (event_definition_version_id) REFERENCES public.event_definition_versions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.event_metric_mappings
    ADD CONSTRAINT event_metric_mappings_metric_definition_version_id_fkey FOREIGN KEY (metric_definition_version_id) REFERENCES public.metric_definition_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.events
    ADD CONSTRAINT fk_events_definition_version_id FOREIGN KEY (definition_version_id) REFERENCES public.event_definition_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.measurements
    ADD CONSTRAINT fk_measurements_metric_definition_version FOREIGN KEY (metric_definition_version_id) REFERENCES public.metric_definition_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.metric_observations
    ADD CONSTRAINT fk_metric_observations_definition_version FOREIGN KEY (metric_definition_version_id) REFERENCES public.metric_definition_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.metric_observations
    ADD CONSTRAINT fk_metric_observations_source_entry FOREIGN KEY (source_entry_id) REFERENCES public.events(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.inbound_messages
    ADD CONSTRAINT inbound_messages_supersedes_id_fkey FOREIGN KEY (supersedes_id) REFERENCES public.inbound_messages(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.measurement_history
    ADD CONSTRAINT measurement_history_metric_definition_version_id_fkey FOREIGN KEY (metric_definition_version_id) REFERENCES public.metric_definition_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.measurement_revisions
    ADD CONSTRAINT measurement_revisions_metric_definition_version_id_fkey FOREIGN KEY (metric_definition_version_id) REFERENCES public.metric_definition_versions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.message_delivery_receipts
    ADD CONSTRAINT message_delivery_receipts_outbox_message_id_fkey FOREIGN KEY (outbox_message_id) REFERENCES public.outbox_messages(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.metric_definition_versions
    ADD CONSTRAINT metric_definition_versions_definition_id_fkey FOREIGN KEY (definition_id) REFERENCES public.metric_definitions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.metric_definitions
    ADD CONSTRAINT metric_definitions_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.module_configs
    ADD CONSTRAINT module_configs_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_conversation_id_fkey FOREIGN KEY (conversation_id) REFERENCES public.conversations(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_inbound_message_id_fkey FOREIGN KEY (inbound_message_id) REFERENCES public.inbound_messages(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.outbox_messages
    ADD CONSTRAINT outbox_messages_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.source_connections
    ADD CONSTRAINT source_connections_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.tracker_configs
    ADD CONSTRAINT tracker_configs_definition_id_fkey FOREIGN KEY (definition_id) REFERENCES public.event_definitions(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.tracker_configs
    ADD CONSTRAINT tracker_configs_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.people(id) ON DELETE CASCADE;
