import asyncio
import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from garmin_ai.accounts import (
    AccountMismatch,
    BindingConfirmationRequired,
    SecondOwnerRejected,
    apply_instance_settings,
    bind_account,
    bind_channel,
    create_owner,
    owner,
    profile_fingerprint,
)
from garmin_ai.config import ApiToken, Settings
from garmin_ai.definitions import ensure_system_definitions
from garmin_ai.metric_definitions import ensure_system_metric_definitions
from garmin_ai.models import AppState, Base, ChannelBinding, Event, Person, SourceConnection
from garmin_ai.operations import export_database, restore_database
from garmin_ai.personal_goals import KEY, GoalSelection, select_goals


def test_erased_database_still_exposes_not_ready_status(db, db_engine):
    from garmin_ai.api import create_app

    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()

    with TestClient(create_app(Settings(), db_engine)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "Storage disabled after erasure"


def test_live_api_reinitializes_identity_after_storage_is_erased_and_resumed(db_engine, tmp_path):
    from garmin_ai.api import create_app
    from garmin_ai.operations import erase_all

    settings = Settings(
        locale="en-US",
        timezone="UTC",
        telegram_user_id=42,
        data_dir=tmp_path / "data",
        lock_dir=tmp_path / "locks",
        token_dir=tmp_path / "tokens",
    )
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM app_state WHERE key='maintenance:erased'"))
    with TestClient(create_app(settings, db_engine)) as client:
        initial = client.get("/health/ready")
        assert initial.status_code == 200, initial.text
        erase_all(db_engine, settings, "ERASE ALL LOCAL HEALTH DATA")
        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM app_state WHERE key='maintenance:erased'"))

        assert client.get("/health/ready").status_code == 200

    with Session(db_engine) as session:
        person = session.scalar(select(Person))
        binding = session.scalar(select(ChannelBinding))
        assert person is not None
        assert (person.locale, person.timezone) == ("en-US", "UTC")
        assert binding is not None and binding.external_id == "42"


def test_api_health_stays_available_before_identity_migration(db_engine):
    from garmin_ai.api import create_app

    isolated = create_engine(
        db_engine.url,
        connect_args={"options": "-c search_path=pg_catalog"},
        hide_parameters=True,
    )
    try:
        with TestClient(create_app(Settings(), isolated)) as client:
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 503
    finally:
        isolated.dispose()


def test_readiness_retries_failed_settings_materialization(db, db_engine, monkeypatch):
    import garmin_ai.accounts
    from garmin_ai.api import create_app

    original = garmin_ai.accounts.apply_instance_settings
    attempts = 0

    def transient(session, settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLAlchemyError("synthetic transient failure")
        return original(session, settings)

    monkeypatch.setattr(garmin_ai.accounts, "apply_instance_settings", transient)
    with TestClient(create_app(Settings(locale="en-US", timezone="UTC"), db_engine)) as client:
        assert client.get("/health/ready").status_code == 200

    db.expire_all()
    assert attempts == 2
    assert (owner(db).locale, owner(db).timezone) == ("en-US", "UTC")


def test_delayed_binding_mismatch_keeps_readiness_unavailable(db, db_engine, monkeypatch):
    import garmin_ai.accounts
    from garmin_ai.api import create_app

    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="1",
        confirmed=True,
    )
    db.commit()
    original = garmin_ai.accounts.apply_instance_settings
    attempts = 0

    def unavailable_then_mismatch(session, settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLAlchemyError("synthetic transient failure")
        return original(session, settings)

    monkeypatch.setattr(
        garmin_ai.accounts,
        "apply_instance_settings",
        unavailable_then_mismatch,
    )
    with TestClient(create_app(Settings(telegram_user_id=2), db_engine)) as client:
        response = client.get("/health/ready")

    assert attempts == 2
    assert response.status_code == 503
    assert response.json()["detail"] == "Database unavailable or not migrated"


def test_startup_binding_mismatch_fails_fast(db, db_engine):
    from garmin_ai.api import create_app

    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="1",
        confirmed=True,
    )
    db.commit()
    with pytest.raises(AccountMismatch):
        create_app(Settings(telegram_user_id=2), db_engine)


def test_webhook_retries_identity_materialization_before_accepting_update(
    db, db_engine, monkeypatch
):
    import garmin_ai.accounts
    from garmin_ai.api import create_app
    from garmin_ai.models import TelegramUpdate

    original = garmin_ai.accounts.apply_instance_settings
    attempts = 0

    def unavailable_then_mismatch(session, settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLAlchemyError("synthetic transient failure")
        return original(session, settings)

    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="1",
        confirmed=True,
    )
    db.commit()
    monkeypatch.setattr(
        garmin_ai.accounts,
        "apply_instance_settings",
        unavailable_then_mismatch,
    )
    settings = Settings(
        telegram_user_id=2,
        telegram_webhook_secret="synthetic-webhook-secret",
    )
    with TestClient(create_app(settings, db_engine)) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic-webhook-secret"},
            json={
                "update_id": 99,
                "message": {
                    "message_id": 99,
                    "from": {"id": 2},
                    "chat": {"id": 2, "type": "private"},
                    "text": "must not be accepted",
                },
            },
        )

    assert response.status_code == 503
    assert attempts == 2
    assert db.get(TelegramUpdate, 99) is None


@pytest.mark.parametrize("route", ["events", "wearable"])
def test_all_database_writes_wait_for_identity_materialization(db, db_engine, monkeypatch, route):
    import garmin_ai.accounts
    from garmin_ai.api import create_app

    original = garmin_ai.accounts.apply_instance_settings
    attempts = 0

    def unavailable_then_mismatch(session, settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SQLAlchemyError("synthetic transient failure")
        return original(session, settings)

    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="1",
        confirmed=True,
    )
    db.commit()
    monkeypatch.setattr(
        garmin_ai.accounts,
        "apply_instance_settings",
        unavailable_then_mismatch,
    )
    key = "synthetic-api-key-with-at-least-32-chars"
    wearable_key = "synthetic-wearable-key-at-least-32-chars"
    settings = Settings(
        telegram_user_id=2,
        api_key=SecretStr(key),
        api_tokens=[
            ApiToken(
                key=wearable_key,
                scopes={"write:wearable"},
                wearable_device_id=UUID("00000000-0000-4000-8000-000000000002"),
            )
        ],
    )
    with TestClient(create_app(settings, db_engine)) as client:
        if route == "events":
            response = client.post(
                "/events",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "start": "2026-09-20T12:00:00Z",
                    "timezone": "UTC",
                    "payload": {"type": "note", "text": "must not be accepted"},
                },
            )
        else:
            response = client.post(
                "/wearable/marks",
                headers={"Authorization": f"Bearer {wearable_key}"},
                json={
                    "marks": [
                        {
                            "id": "00000000-0000-4000-8000-000000000001",
                            "device_time": "2026-09-20T12:00:00Z",
                            "timezone": "UTC",
                            "payload": {"type": "caffeine", "beverage": "synthetic"},
                        }
                    ]
                },
            )

    assert response.status_code == 503
    assert attempts == 2
    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_mcp_initialization_rejects_a_conflicting_owner_binding(db, db_engine):
    from garmin_ai.accounts import AccountMismatch
    from garmin_ai.mcp_server import initialize_identity

    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="1",
        confirmed=True,
    )
    db.commit()

    with pytest.raises(AccountMismatch):
        initialize_identity(db_engine, Settings(telegram_user_id=2, mcp_enable_writes=True))

    assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_mcp_revalidates_identity_for_each_tool_call(db, db_engine):
    from mcp import types

    from garmin_ai.mcp_server import build_server

    bind_channel(
        db,
        channel="telegram",
        channel_instance_id="primary",
        external_id="1",
        confirmed=True,
    )
    db.commit()
    server = build_server(
        db_engine,
        enable_writes=True,
        identity_settings=Settings(telegram_user_id=2),
    )
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(name="data_freshness", arguments={})
    )

    result = asyncio.run(server.request_handlers[types.CallToolRequest](request))

    assert result.root.isError
    assert "AccountMismatch" in result.root.content[0].text


def test_rejected_second_runtime_does_not_apply_instance_settings(monkeypatch):
    from garmin_ai import runtime

    events = []

    class Connection:
        def execution_options(self, **kwargs):
            return self

        def scalar(self, statement):
            events.append("lock")
            return False

        def close(self):
            events.append("close")

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            events.append("dispose")

    def forbidden_transaction(engine):
        pytest.fail("Rejected runtime must not open the settings transaction")

    monkeypatch.setattr(runtime, "make_engine", lambda settings: Engine())
    monkeypatch.setattr(runtime, "transaction", forbidden_transaction)

    with pytest.raises(RuntimeError, match="Another Garmin AI runtime"):
        asyncio.run(runtime._run(Settings()))

    assert events == ["lock", "close", "dispose"]


def test_clean_store_has_owner_without_external_accounts(db):
    person = owner(db)

    assert person.id is not None
    assert person.locale == "ru"
    assert person.units == "metric"
    assert db.scalar(select(func.count()).select_from(Person)) == 1
    assert db.scalar(select(func.count()).select_from(SourceConnection)) == 0
    assert db.scalar(select(func.count()).select_from(ChannelBinding)) == 0


def test_system_definition_bootstrap_does_not_require_legacy_enrollment(db):
    ensure_system_definitions(db)
    ensure_system_metric_definitions(db)
    fingerprint = profile_fingerprint({"profileId": 123456})

    binding = bind_account(db, fingerprint)

    assert binding["fingerprint"] == fingerprint


def test_instance_profile_is_independent_from_garmin_and_telegram(db):
    person = apply_instance_settings(db, Settings(locale="en-US", timezone="UTC", units="imperial"))

    assert (person.locale, person.timezone, person.units) == ("en-US", "UTC", "imperial")
    assert db.scalar(select(func.count()).select_from(SourceConnection)) == 0
    assert db.scalar(select(func.count()).select_from(ChannelBinding)) == 0


def test_second_owner_is_explicitly_rejected_and_recreated_install_gets_new_id(db):
    first = owner(db)
    with db.begin_nested():
        try:
            create_owner(db)
        except SecondOwnerRejected:
            pass
        else:
            raise AssertionError("A second owner was accepted")

    db.delete(first)
    db.flush()
    replacement = owner(db)

    assert replacement.id != first.id


def test_concurrent_owner_bootstrap_returns_the_same_created_owner(db, db_engine):
    db.execute(text("DELETE FROM people"))
    db.commit()

    def load_owner(_):
        with Session(db_engine) as session, session.begin():
            return owner(session).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(load_owner, range(2)))

    assert identities[0] == identities[1]
    assert db.scalar(select(func.count()).select_from(Person)) == 1


def test_channel_binding_requires_explicit_confirmation_and_preserves_opaque_id(db):
    try:
        bind_channel(
            db,
            channel="synthetic",
            channel_instance_id="private-installation",
            external_id="0042",
        )
    except BindingConfirmationRequired:
        pass
    else:
        raise AssertionError("An unconfirmed channel was linked")

    binding = bind_channel(
        db,
        channel="synthetic",
        channel_instance_id="private-installation",
        external_id="0042",
        confirmed=True,
    )
    assert binding.external_id == "0042"
    assert binding.owner_id == owner(db).id
    try:
        bind_channel(
            db,
            channel="synthetic",
            channel_instance_id="private-installation",
            external_id="42",
            confirmed=True,
        )
    except AccountMismatch:
        pass
    else:
        raise AssertionError("A channel was silently rebound to another identity")


def test_legacy_telegram_configuration_becomes_explicit_channel_binding(db):
    person = apply_instance_settings(db, Settings(telegram_user_id=42, timezone="UTC"))
    binding = db.scalar(select(ChannelBinding))

    assert binding.owner_id == person.id
    assert binding.channel == "telegram"
    assert binding.channel_instance_id == "primary"
    assert binding.external_id == "42"
    assert binding.confirmation_method == "legacy_configuration"


def test_existing_channel_binding_needs_no_exclusive_owner_lock(db, db_engine):
    apply_instance_settings(db, Settings(telegram_user_id=42))
    db.commit()
    with db_engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(72104627)"))
        with Session(db_engine) as session, session.begin():
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            apply_instance_settings(session, Settings(telegram_user_id=42))
        holder.rollback()


def test_garmin_fingerprint_is_an_owner_source_connection(db):
    fingerprint = profile_fingerprint({"profileId": 12345})
    legacy = bind_account(db, fingerprint)
    connection = db.scalar(select(SourceConnection))

    assert connection.owner_id == owner(db).id
    assert connection.provider == "garmin"
    assert connection.namespace == "socialProfile.profileId:v1"
    assert connection.external_id == fingerprint
    assert connection.details["instance_id"] == legacy["instance_id"]


def test_tracker_preferences_are_owned_by_the_internal_person(db):
    person = owner(db)
    select_goals(db, GoalSelection(revision=0, goals=["sleep"]))

    assert db.get(AppState, KEY).value["owner_id"] == str(person.id)


def test_export_materializes_configured_telegram_owner(db, db_engine, tmp_path):
    archive = tmp_path / "configured.gz"
    export_database(db_engine, archive, settings=Settings(telegram_user_id=42))

    binding = db.scalar(select(ChannelBinding))
    assert binding is not None and binding.external_id == "42"
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    assert any(
        row.get("table") == "channel_bindings" and row["row"]["external_id"] == "42" for row in rows
    )


def test_restore_rejects_active_runtime_independent_of_file_lock(db, db_engine, tmp_path):
    archive = tmp_path / "source.gz"
    export_database(db_engine, archive)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as runtime:
        runtime.execute(text("SELECT pg_advisory_lock(72104620)"))
        try:
            with pytest.raises(ValueError, match="Stop the runtime"):
                restore_database(db_engine, archive)
        finally:
            runtime.execute(text("SELECT pg_advisory_unlock(72104620)"))


def test_legacy_export_restore_creates_owner_and_converts_garmin_binding(db, db_engine, tmp_path):
    fingerprint = profile_fingerprint({"profileId": 67890})
    bind_account(db, fingerprint)
    select_goals(db, GoalSelection(revision=0, goals=["sleep"]))
    db.commit()
    current = tmp_path / "current.gz"
    legacy = tmp_path / "legacy.gz"
    export_database(db_engine, current)

    with (
        gzip.open(current, "rt", encoding="utf-8") as source,
        gzip.open(legacy, "wt", encoding="utf-8") as destination,
    ):
        for line in source:
            record = json.loads(line)
            if record.get("table") in {
                "people",
                "source_connections",
                "channel_bindings",
                "event_definitions",
                "event_definition_versions",
            }:
                continue
            if record.get("table") == "app_state" and record["row"]["key"] == KEY:
                record["row"]["value"].pop("owner_id", None)
            if "revision" in record:
                record["revision"] = "d31e572abc90"
            if "counts" in record:
                for table in (
                    "people",
                    "source_connections",
                    "channel_bindings",
                    "event_definitions",
                    "event_definition_versions",
                ):
                    record["counts"].pop(table, None)
            destination.write(json.dumps(record) + "\n")

    names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    restored = restore_database(db_engine, legacy)
    db.expire_all()

    restored_owner = db.scalar(select(Person))
    restored_connection = db.scalar(select(SourceConnection))
    assert restored["people"] == 1
    assert restored_owner is not None
    assert restored_connection.owner_id == restored_owner.id
    assert restored_connection.external_id == fingerprint
    assert db.get(AppState, KEY).value["owner_id"] == str(restored_owner.id)
    assert db.get(AppState, "registry:system:contract_digest") is not None
    assert restored["app_state"] == db.scalar(select(func.count()).select_from(AppState))
