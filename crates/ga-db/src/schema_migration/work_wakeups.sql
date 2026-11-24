-- Hints only: rows, leases, deduplication and retry deadlines remain durable.
-- Static payloads carry no owner/message data and coalesce within a transaction.
CREATE FUNCTION public.ga_work_topic(job_kind text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT CASE job_kind
        WHEN 'telegram_native_inbound' THEN 'telegram_inbound'
        WHEN 'telegram_native_ack' THEN 'telegram_acknowledgements'
        WHEN 'telegram_storage_notice' THEN 'telegram_outbox'
        WHEN 'telegram_connection_notice' THEN 'telegram_outbox'
        WHEN 'garmin_endpoint' THEN 'garmin'
        WHEN 'garmin_activities' THEN 'garmin'
        WHEN 'garmin_fit' THEN 'garmin'
        WHEN 'raw_replay' THEN 'replay'
        WHEN 'backup' THEN 'maintenance'
        WHEN 'storage_check' THEN 'maintenance'
        WHEN 'channel_initiatives' THEN 'reminders'
        WHEN 'source_plugin_poll' THEN 'extensions'
        ELSE NULL
    END
$$;

CREATE FUNCTION public.ga_notify_job_work() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    topic text;
BEGIN
    IF TG_OP='DELETE' THEN
        topic := public.ga_work_topic(OLD.kind);
        IF topic IS NOT NULL AND OLD.status IN ('pending','running') THEN
            PERFORM pg_catalog.pg_notify('ga_work_ready',topic);
        END IF;
        RETURN OLD;
    END IF;
    topic := public.ga_work_topic(NEW.kind);
    IF topic IS NULL THEN
        RETURN NEW;
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.status='pending' THEN
            PERFORM pg_catalog.pg_notify('ga_work_ready',topic);
            IF NEW.kind='telegram_native_inbound' THEN
                -- Only a new message requests a fresh Garmin planning pass.
                PERFORM pg_catalog.pg_notify('ga_work_ready','garmin_schedule');
            END IF;
        ELSIF NEW.status='running' THEN
            PERFORM pg_catalog.pg_notify('ga_work_ready','deadlines');
        END IF;
    ELSIF NEW.status='pending' AND (
        NEW.status IS DISTINCT FROM OLD.status OR NEW.kind IS DISTINCT FROM OLD.kind
        OR NEW.run_at IS DISTINCT FROM OLD.run_at OR NEW.payload IS DISTINCT FROM OLD.payload
    ) THEN
        -- Includes a handoff between local, voice and model processing of one job.
        PERFORM pg_catalog.pg_notify('ga_work_ready',topic);
    ELSIF NEW.status NOT IN ('pending','running') AND NEW.status IS DISTINCT FROM OLD.status THEN
        -- Completing an earlier job can release the next ordered message.
        PERFORM pg_catalog.pg_notify('ga_work_ready',topic);
        IF topic='garmin' THEN
            PERFORM pg_catalog.pg_notify('ga_work_ready','maintenance');
        ELSIF topic='maintenance' THEN
            PERFORM pg_catalog.pg_notify('ga_work_ready','garmin');
        END IF;
    ELSIF NEW.status='running' AND (
        NEW.status IS DISTINCT FROM OLD.status OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
        OR NEW.kind IS DISTINCT FROM OLD.kind
    ) THEN
        -- A new claim creates an expiry timer, without waking queue consumers.
        -- Pure lease renewal does not notify; the old expiry refreshes its timer.
        PERFORM pg_catalog.pg_notify('ga_work_ready','deadlines');
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.ga_notify_outbox_work() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    ready boolean := false;
BEGIN
    IF TG_OP='DELETE' THEN
        ready := OLD.state IN ('queued','sending');
    ELSIF TG_OP='INSERT' THEN
        ready := NEW.state='queued';
        IF NEW.state='sending' THEN
            PERFORM pg_catalog.pg_notify('ga_work_ready','deadlines');
        END IF;
    ELSIF NEW.state='queued' AND (
        NEW.state IS DISTINCT FROM OLD.state
        OR NEW.next_attempt_at IS DISTINCT FROM OLD.next_attempt_at
        OR NEW.intent IS DISTINCT FROM OLD.intent
        OR NEW.memory_fence IS DISTINCT FROM OLD.memory_fence
    ) THEN
        ready := true;
    ELSIF NEW.state NOT IN ('queued','sending') AND NEW.state IS DISTINCT FROM OLD.state THEN
        ready := true;
    ELSIF NEW.state='sending' AND (
        NEW.state IS DISTINCT FROM OLD.state OR NEW.lease_token IS DISTINCT FROM OLD.lease_token
    ) THEN
        PERFORM pg_catalog.pg_notify('ga_work_ready','deadlines');
    END IF;
    IF ready THEN
        PERFORM pg_catalog.pg_notify('ga_work_ready','telegram_outbox');
        PERFORM pg_catalog.pg_notify('ga_work_ready','extensions');
    END IF;
    IF TG_OP='DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

-- Row triggers are essential: a zero-row claim must not notify itself forever.
CREATE TRIGGER ga_jobs_work_wakeup
AFTER INSERT OR UPDATE OR DELETE ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.ga_notify_job_work();

CREATE TRIGGER ga_outbox_work_wakeup
AFTER INSERT OR UPDATE OR DELETE ON public.outbox_messages
FOR EACH ROW EXECUTE FUNCTION public.ga_notify_outbox_work();
