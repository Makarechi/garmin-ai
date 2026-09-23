"""Single-owner source binding. Fingerprints are identifiers, never credentials."""

import hashlib
import json
import re
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select, text

from garmin_ai.canonical_events import CANONICAL_VALIDATION_KEY
from garmin_ai.db import transaction, writer_guard
from garmin_ai.models import (
    AppState,
    Base,
    ChannelBinding,
    EventDefinition,
    MetricDefinition,
    Person,
    SourceConnection,
)

BINDING_KEY = "account:garmin"
GARMIN_NAMESPACE = "socialProfile.profileId:v1"
PRIMARY_CHANNEL_INSTANCE = "primary"


class AccountError(RuntimeError):
    pass


class AccountMismatch(AccountError):
    pass


class AccountEnrollmentRequired(AccountError):
    pass


class SecondOwnerRejected(AccountError):
    pass


class BindingConfirmationRequired(AccountError):
    pass


def create_owner(session, *, locale="ru", timezone="Europe/Bratislava", units="metric"):
    session.execute(text("SELECT pg_advisory_xact_lock(72104627)"))
    if session.scalar(select(Person.id).limit(1)) is not None:
        raise SecondOwnerRejected("This instance already has its sole owner")
    person = Person(locale=locale, timezone=timezone, units=units)
    session.add(person)
    session.flush()
    return person


def owner(session):
    person = session.scalar(select(Person).limit(1))
    if person is not None:
        return person
    session.execute(text("SELECT pg_advisory_xact_lock(72104627)"))
    person = session.scalar(select(Person).limit(1))
    if person is not None:
        return person
    person = Person()
    session.add(person)
    session.flush()
    return person


def bind_source_connection(
    session,
    *,
    provider,
    namespace,
    external_id,
    confirmation_method,
    details=None,
):
    session.execute(text("SELECT pg_advisory_xact_lock(72104627)"))
    person = owner(session)
    external_id = str(external_id)
    connection = session.scalar(
        select(SourceConnection).where(
            SourceConnection.owner_id == person.id,
            SourceConnection.provider == provider,
            SourceConnection.namespace == namespace,
        )
    )
    if connection is not None:
        if not secrets.compare_digest(connection.external_id, external_id):
            raise AccountMismatch(f"{provider} account does not match this instance")
        return connection
    connection = SourceConnection(
        owner_id=person.id,
        provider=provider,
        namespace=namespace,
        external_id=external_id,
        confirmation_method=confirmation_method,
        details=details or {},
        confirmed_at=datetime.now(UTC),
    )
    session.add(connection)
    session.flush()
    return connection


def bind_channel(
    session,
    *,
    channel,
    channel_instance_id,
    external_id,
    confirmed=False,
    confirmation_method="explicit_pairing",
):
    person = owner(session)
    channel_instance_id = str(channel_instance_id)
    external_id = str(external_id)
    query = select(ChannelBinding).where(
        ChannelBinding.owner_id == person.id,
        ChannelBinding.channel == channel,
        ChannelBinding.channel_instance_id == channel_instance_id,
    )
    binding = session.scalar(query)
    if binding is not None:
        if not secrets.compare_digest(binding.external_id, external_id):
            raise AccountMismatch(f"{channel} owner does not match this instance")
        return binding
    session.execute(text("SELECT pg_advisory_xact_lock(72104627)"))
    binding = session.scalar(query)
    if binding is not None:
        if not secrets.compare_digest(binding.external_id, external_id):
            raise AccountMismatch(f"{channel} owner does not match this instance")
        return binding
    if not confirmed:
        raise BindingConfirmationRequired("A new channel requires explicit owner confirmation")
    binding = ChannelBinding(
        owner_id=person.id,
        channel=channel,
        channel_instance_id=channel_instance_id,
        external_id=external_id,
        confirmation_method=confirmation_method,
        confirmed_at=datetime.now(UTC),
    )
    session.add(binding)
    session.flush()
    return binding


def apply_instance_settings(session, settings):
    person = owner(session)
    # Configuration provides bootstrap defaults.  Once onboarding has stored
    # explicit owner preferences, process restarts must not replace them.
    if session.get(AppState, "preferences:onboarding") is None:
        person.locale = settings.locale
        person.timezone = settings.timezone
        person.units = settings.units
    if settings.telegram_user_id > 0:
        from garmin_ai.integrations import channel_instance_id, configured_instance

        telegram_instance = configured_instance(settings, "channel", "telegram")
        if not settings.integrations or telegram_instance is not None:
            bind_channel(
                session,
                channel="telegram",
                channel_instance_id=channel_instance_id(telegram_instance),
                external_id=str(settings.telegram_user_id),
                confirmed=True,
                confirmation_method="legacy_configuration",
            )
    session.flush()
    return person


def effective_owner_settings(session, settings):
    """Overlay persisted owner preferences on operational configuration."""

    person = owner(session)
    if session.get(AppState, "preferences:onboarding") is None:
        person.locale = settings.locale
        person.timezone = settings.timezone
        person.units = settings.units
    session.info["locale"] = person.locale
    session.info["timezone"] = person.timezone
    session.info["units"] = person.units
    return settings.model_copy(
        update={
            "locale": person.locale,
            "timezone": person.timezone,
            "units": person.units,
        }
    )


def profile_fingerprint(profile):
    identity = profile.get("profileId") if isinstance(profile, dict) else None
    if isinstance(identity, bool) or not isinstance(identity, (int, str)):
        raise AccountError("Authenticated stable profile identity unavailable")
    text = str(identity)
    if len(text) > 19 or not text.isascii() or not text.isdecimal() or not 0 < int(text) < 2**63:
        raise AccountError("Authenticated stable profile identity unavailable")
    return hashlib.sha256(f"garmin:socialProfile:profileId:v1:{int(text)}".encode()).hexdigest()


def validate_fingerprint(fingerprint):
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise AccountError("Account fingerprint unavailable")


def check_retained_archive(archive_root, *, confirm_existing_owner=False):
    if archive_root is not None and archive_root.exists() and not confirm_existing_owner:
        if next(archive_root.rglob("*"), None) is not None:
            raise AccountEnrollmentRequired("Confirm ownership of retained raw storage locally")


def verify_file_probe(archive_root, report_path, fingerprint):
    validate_fingerprint(fingerprint)
    if next(archive_root.rglob("*"), None) is None:
        return
    try:
        report = json.loads(report_path.read_text())
        previous = report.get("account_fingerprint")
        validate_fingerprint(previous)
    except (OSError, ValueError, AttributeError, AccountError):
        raise AccountEnrollmentRequired(
            "Retained probe ownership requires local enrollment"
        ) from None
    if not secrets.compare_digest(previous, fingerprint):
        raise AccountMismatch("Retained probe belongs to another Garmin account")


def existing_account(session, fingerprint):
    validate_fingerprint(fingerprint)
    person = owner(session)
    connection = session.scalar(
        select(SourceConnection).where(
            SourceConnection.owner_id == person.id,
            SourceConnection.provider == "garmin",
            SourceConnection.namespace == GARMIN_NAMESPACE,
        )
    )
    if connection is not None and not secrets.compare_digest(connection.external_id, fingerprint):
        raise AccountMismatch("Garmin account does not match this instance")
    binding = session.get(AppState, BINDING_KEY, populate_existing=True)
    if binding:
        expected = binding.value.get("fingerprint")
        validate_fingerprint(expected)
        if not secrets.compare_digest(expected, fingerprint):
            raise AccountMismatch("Garmin account does not match this instance")
        if connection is None:
            bind_source_connection(
                session,
                provider="garmin",
                namespace=GARMIN_NAMESPACE,
                external_id=fingerprint,
                confirmation_method="legacy_account_binding",
                details={
                    key: binding.value.get(key)
                    for key in ("instance_id", "identity_contract")
                    if binding.value.get(key) is not None
                },
            )
        return dict(binding.value)
    if connection is not None:
        value = {
            "instance_id": connection.details.get("instance_id") or str(person.id),
            "fingerprint": fingerprint,
            "identity_contract": GARMIN_NAMESPACE,
        }
        session.add(AppState(key=BINDING_KEY, value=value))
        session.flush()
        return value
    return None


def bind_account(session, fingerprint, *, confirm_existing_owner=False, archive_root=None):
    validate_fingerprint(fingerprint)
    writer_guard(session, enrollment=True)
    binding = existing_account(session, fingerprint)
    if binding is not None:
        return binding
    check_retained_archive(archive_root, confirm_existing_owner=confirm_existing_owner)
    # Operational jobs alone do not imply an established owner. Everything else,
    # including diary, audit and raw provenance, requires explicit legacy enrollment.
    ignored_bootstrap_tables = {
        "app_state",
        "jobs",
        "people",
        "source_connections",
        "channel_bindings",
        "event_definitions",
        "event_definition_versions",
        "metric_definitions",
        "metric_definition_versions",
        "module_configs",
    }
    populated = any(
        session.scalar(select(1).select_from(table).limit(1)) is not None
        for table in Base.metadata.sorted_tables
        if table.name not in ignored_bootstrap_tables
    )
    populated = populated or (
        session.scalar(
            select(EventDefinition.id).where(EventDefinition.namespace != "system").limit(1)
        )
        is not None
    )
    populated = populated or (
        session.scalar(
            select(MetricDefinition.id).where(MetricDefinition.namespace != "system").limit(1)
        )
        is not None
    )
    populated = (
        populated
        or session.scalar(
            select(AppState.key)
            .where(
                AppState.key.not_in(
                    {
                        "runtime:heartbeat",
                        "telegram:offset",
                        "proactive:enabled",
                        "integration:garmin",
                        "proactive:generation",
                        "backup:last_success",
                        "registry:system:contract_digest",
                        "registry:metric:catalog_digest",
                        CANONICAL_VALIDATION_KEY,
                    }
                ),
                ~AppState.key.startswith("outbox:auth:"),
                ~AppState.key.startswith("outbox:account-binding:"),
            )
            .limit(1)
        )
        is not None
    )
    if populated and not confirm_existing_owner:
        raise AccountEnrollmentRequired("Confirm the existing owner using local enrollment")
    value = {
        "instance_id": str(uuid4()),
        "fingerprint": fingerprint,
        "identity_contract": "socialProfile.profileId:v1",
    }
    session.add(AppState(key=BINDING_KEY, value=value))
    bind_source_connection(
        session,
        provider="garmin",
        namespace=GARMIN_NAMESPACE,
        external_id=fingerprint,
        confirmation_method="authenticated_profile",
        details={
            "instance_id": value["instance_id"],
            "identity_contract": value["identity_contract"],
        },
    )
    session.flush()
    return value


def ensure_account(
    engine, fingerprint, *, confirm_existing_owner=False, archive_root=None, before_commit=None
):
    with transaction(engine) as session:
        result = existing_account(session, fingerprint)
        if result is not None:
            if before_commit is not None:
                before_commit()
            return result
    # Release the shared transaction before acquiring the enrollment lock.
    # bind_account rechecks the binding under that exclusive lock.
    with transaction(engine, enrollment=True) as session:
        result = bind_account(
            session,
            fingerprint,
            confirm_existing_owner=confirm_existing_owner,
            archive_root=archive_root,
        )
        if before_commit is not None:
            before_commit()
        return result


def verify_setup_account(
    engine, fingerprint, *, confirm_existing_owner=False, archive_root=None, before_commit=None
):
    """For login/probe under standalone_files only; never authorizes canonical ingestion."""
    validate_fingerprint(fingerprint)
    with engine.connect() as connection:
        if connection.scalar(text("SELECT to_regclass('app_state')")) is None:
            check_retained_archive(archive_root, confirm_existing_owner=confirm_existing_owner)
            # An entirely unmigrated store has no owner to compare yet. A partial
            # schema is not evidence of an empty installation.
            if any(
                connection.scalar(select(func.to_regclass(table.name))) is not None
                for table in Base.metadata.sorted_tables
            ):
                raise AccountEnrollmentRequired("Migrate and verify the existing owner")
            if before_commit is not None:
                before_commit()
            return None
        if connection.scalar(text("SELECT to_regclass('people')")) is None:
            raise AccountEnrollmentRequired("Migrate the database before verifying its owner")
        if connection.scalar(text("SELECT 1 FROM app_state WHERE key='maintenance:erased'")):
            if before_commit is not None:
                before_commit()
            return None
    return ensure_account(
        engine,
        fingerprint,
        confirm_existing_owner=confirm_existing_owner,
        archive_root=archive_root,
        before_commit=before_commit,
    )


@contextmanager
def account_transaction(engine, fingerprint, *, archive_root=None):
    ensure_account(engine, fingerprint, archive_root=archive_root)
    with transaction(engine) as session:
        if existing_account(session, fingerprint) is None:
            raise AccountEnrollmentRequired("Owner binding disappeared; enroll locally")
        yield session
