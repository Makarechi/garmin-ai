import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from garmin_ai.accounts import (
    BINDING_KEY,
    AccountEnrollmentRequired,
    AccountError,
    AccountMismatch,
    bind_account,
    ensure_account,
    profile_fingerprint,
)
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.events import EventInput, create_event
from garmin_ai.models import AppState, Measurement, SourcePayload
from garmin_ai.sync import import_probe, run_garmin_job

A = profile_fingerprint({"profileId": 101})
B = profile_fingerprint({"profileId": 202})


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"displayName": "owner"},
        {"profileId": True},
        {"profileId": -1},
        {"profileId": "email@example.invalid"},
        {"profileId": 1.5},
        {"profileId": "１２３"},
    ],
)
def test_identity_never_falls_back_to_mutable_names(profile):
    with pytest.raises(AccountError):
        profile_fingerprint(profile)
    assert A == profile_fingerprint({"profileId": "00101", "displayName": "changed"})


def test_empty_enrollment_is_idempotent_and_cannot_rebind(db):
    first = bind_account(db, A)
    assert first == bind_account(db, A)
    with pytest.raises(AccountMismatch):
        bind_account(db, B, confirm_existing_owner=True)
    assert db.get(AppState, BINDING_KEY).value == first


@pytest.mark.parametrize("kind", ["diary", "conversation"])
def test_populated_store_requires_explicit_legacy_enrollment(db, kind):
    if kind == "diary":
        create_event(
            db,
            EventInput(
                start=datetime(2026, 9, 1, tzinfo=UTC),
                payload={"type": "note", "description": "synthetic"},
            ),
            actor="test",
        )
    else:
        db.add(AppState(key="conversation:pending", value={"text": "synthetic"}))
        db.flush()
    with pytest.raises(AccountEnrollmentRequired):
        bind_account(db, A)
    assert db.get(AppState, BINDING_KEY) is None
    assert bind_account(db, A, confirm_existing_owner=True)["fingerprint"] == A


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("garmin_endpoint", {"endpoint": "heart_rate", "key": "2026-09-10"}),
        ("garmin_activities", {"offset": 0, "since": "2026-09-01"}),
        ("garmin_fit", {"activity_id": "synthetic"}),
    ],
)
def test_wrong_account_sync_fails_before_fetch_archive_or_write(
    db, db_engine, tmp_path, kind, payload
):
    ensure_account(db_engine, A)
    reader = SimpleNamespace(account_fingerprint=lambda: B)
    archive = LocalArchive(tmp_path)
    with pytest.raises(AccountMismatch):
        run_garmin_job(db_engine, reader, archive, Settings(), kind, payload)
    assert db.scalar(select(func.count()).select_from(SourcePayload)) == 0
    assert db.scalar(select(func.count()).select_from(Measurement)) == 0
    assert not list(tmp_path.rglob("*.json"))


def test_probe_import_requires_matching_provenance_even_with_legacy_confirmation(
    db, db_engine, tmp_path
):
    ensure_account(db_engine, A)
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"account_fingerprint": B, "requests": []}))
    with pytest.raises(AccountMismatch):
        import_probe(
            db_engine,
            LocalArchive(tmp_path / "raw"),
            Settings(),
            path,
            confirmed_legacy_fingerprint=A,
        )
    path.write_text(json.dumps({"requests": []}))
    with pytest.raises(AccountError):
        import_probe(db_engine, LocalArchive(tmp_path / "raw"), Settings(), path)
    assert (
        import_probe(
            db_engine,
            LocalArchive(tmp_path / "raw"),
            Settings(),
            path,
            confirmed_legacy_fingerprint=A,
        )["imported"]
        == 0
    )


def test_binding_survives_export_restore_and_rejects_other_account(db, db_engine, tmp_path):
    from sqlalchemy import text

    from garmin_ai.models import Base
    from garmin_ai.operations import export_database, restore_database

    original = ensure_account(db_engine, A)
    path = tmp_path / "export.gz"
    export_database(db_engine, path)
    with db_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE "
                + ", ".join('"' + t.name + '"' for t in Base.metadata.sorted_tables)
                + " RESTART IDENTITY CASCADE"
            )
        )
    restore_database(db_engine, path)
    assert ensure_account(db_engine, A) == original
    with pytest.raises(AccountMismatch):
        ensure_account(db_engine, B)


def test_login_mismatch_preserves_existing_tokens_and_neutralizes_ambient_cache(
    db, db_engine, tmp_path, monkeypatch
):
    import os

    from garmin_ai import cli

    settings = Settings(
        database_url=db_engine.url.render_as_string(hide_password=False),
        data_dir=tmp_path / "data",
        token_dir=tmp_path / "tokens",
        lock_dir=tmp_path / "locks",
    )
    settings.token_dir.mkdir()
    token = settings.token_dir / "garmin_tokens.json"
    token.write_text("synthetic-existing-token")
    ensure_account(db_engine, A)

    class Candidate:
        def __init__(self, **kwargs):
            self.client = self

        def login(self):
            assert "GARMINTOKENS" not in os.environ

        def connectapi(self, path):
            return {"profileId": 202}

        def dump(self, path):
            pytest.fail("Wrong account must never publish candidate tokens")

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "Garmin", Candidate)
    monkeypatch.setattr("builtins.input", lambda _: "synthetic@example.invalid")
    monkeypatch.setattr(cli, "getpass", lambda _: "synthetic")
    monkeypatch.setenv("GARMINTOKENS", "synthetic-unrelated-cache")
    monkeypatch.setattr("sys.argv", ["garmin-ai", "login"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert token.read_text() == "synthetic-existing-token"
    assert os.environ["GARMINTOKENS"] == "synthetic-unrelated-cache"
    assert ensure_account(db_engine, A)["fingerprint"] == A


def test_setup_before_migration_and_after_erasure_does_not_enroll_or_resume(db, db_engine):
    from sqlalchemy import create_engine

    from garmin_ai.accounts import verify_setup_account
    from garmin_ai.db import MaintenanceMode

    fresh = create_engine(db_engine.url, connect_args={"options": "-c search_path=pg_catalog"})
    try:
        assert verify_setup_account(fresh, A) is None
    finally:
        fresh.dispose()
    db.add(AppState(key="maintenance:erased", value={"disabled": True}))
    db.commit()
    assert verify_setup_account(db_engine, B) is None
    assert db.get(AppState, BINDING_KEY) is None
    with pytest.raises(MaintenanceMode):
        ensure_account(db_engine, B)


def test_concurrent_first_owners_cannot_both_enroll(db, db_engine):
    from concurrent.futures import ThreadPoolExecutor

    def enroll(fingerprint):
        try:
            return ensure_account(db_engine, fingerprint)["fingerprint"]
        except AccountMismatch:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enroll, [A, B]))
    assert results.count("rejected") == 1
    assert db.get(AppState, BINDING_KEY).value["fingerprint"] in {A, B}
