import asyncio
import gzip
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text

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
from garmin_ai.config import Settings
from garmin_ai.models import AppState, Base, ChannelBinding, Person, SourceConnection
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
            if record.get("table") in {"people", "source_connections", "channel_bindings"}:
                continue
            if record.get("table") == "app_state" and record["row"]["key"] == KEY:
                record["row"]["value"].pop("owner_id", None)
            if "revision" in record:
                record["revision"] = "d31e572abc90"
            if "counts" in record:
                for table in ("people", "source_connections", "channel_bindings"):
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
