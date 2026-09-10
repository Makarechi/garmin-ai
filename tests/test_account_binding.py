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


def test_bound_ingestion_allows_other_workers_and_telegram_ordering(db, db_engine):
    from sqlalchemy import text

    from garmin_ai.accounts import account_transaction
    from garmin_ai.db import transaction

    ensure_account(db_engine, A)
    with account_transaction(db_engine, A):
        with db_engine.connect() as connection:
            connection.execute(text("SET statement_timeout = '1000ms'"))
            with transaction(connection) as worker:
                assert worker.scalar(text("SELECT pg_try_advisory_xact_lock(72104623)"))
                assert worker.scalar(text("SELECT 1")) == 1


@pytest.mark.parametrize("failure", ["connection", "rate_limit"])
def test_identity_requests_share_reader_circuit_breaker(failure):
    from garminconnect import GarminConnectConnectionError, GarminConnectTooManyRequestsError

    from garmin_ai.garmin import CircuitOpen, GarminReader

    calls = []
    error = (
        GarminConnectConnectionError
        if failure == "connection"
        else GarminConnectTooManyRequestsError
    )

    def profile(path):
        calls.append(path)
        raise error("synthetic")

    reader = GarminReader(
        SimpleNamespace(connectapi=profile), sleep=lambda _: None, clock=lambda: 0
    )
    for _ in range(8):
        with pytest.raises((error, CircuitOpen)):
            reader.account_fingerprint()
    assert len(calls) == (5 if failure == "connection" else 1)
    with pytest.raises(CircuitOpen):
        reader.account_fingerprint()


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


def test_enrollment_does_not_wait_for_proactive_diary_reservation(db, db_engine):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text

    from garmin_ai.db import transaction

    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as reservation:
        reservation.execute(text("SELECT pg_advisory_lock(72104619)"))
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(ensure_account, db_engine, A)
            assert future.result(timeout=3)["fingerprint"] == A
            with transaction(db_engine) as session:
                assert session.get(AppState, BINDING_KEY) is not None
        finally:
            reservation.execute(text("SELECT pg_advisory_unlock(72104619)"))
            pool.shutdown(wait=True)


def test_first_enrollment_still_waits_for_ordinary_owner_writes(db, db_engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event as ThreadEvent

    from garmin_ai.db import transaction

    started = ThreadEvent()

    def enroll():
        started.set()
        return ensure_account(db_engine, A)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(db_engine) as session:
            session.add(AppState(key="synthetic-owner-data", value={"present": True}))
            session.flush()
            future = pool.submit(enroll)
            assert started.wait(3)
            assert not future.done()
        with pytest.raises(AccountEnrollmentRequired):
            future.result(timeout=3)


def test_failed_token_publication_does_not_commit_first_binding(db, db_engine):
    from garmin_ai.accounts import verify_setup_account

    def fail():
        raise OSError("synthetic publication failure")

    with pytest.raises(OSError):
        verify_setup_account(db_engine, A, before_commit=fail)
    assert db.get(AppState, BINDING_KEY) is None
    assert ensure_account(db_engine, B)["fingerprint"] == B


def test_backup_metadata_does_not_imply_an_existing_owner(db, db_engine):
    db.add(AppState(key="backup:last_success", value={"completed_at": "synthetic"}))
    db.commit()
    assert ensure_account(db_engine, A)["fingerprint"] == A


def test_retained_raw_requires_confirmation_before_new_owner_enrollment(db, db_engine, tmp_path):
    archive = LocalArchive(tmp_path / "raw")
    archive.put_json({"synthetic_owner_a_data": True})
    with pytest.raises(AccountEnrollmentRequired, match="retained raw"):
        run_garmin_job(
            db_engine,
            SimpleNamespace(account_fingerprint=lambda: B),
            archive,
            Settings(),
            "garmin_endpoint",
            {"endpoint": "heart_rate", "key": "2026-09-10"},
        )
    assert db.get(AppState, BINDING_KEY) is None
    assert (
        ensure_account(db_engine, A, archive_root=archive.root, confirm_existing_owner=True)[
            "fingerprint"
        ]
        == A
    )


def test_enrollment_waits_for_concurrent_diary_commit(db, db_engine):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event

    from garmin_ai.db import transaction

    started = Event()

    def enroll():
        started.set()
        return ensure_account(db_engine, A)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(db_engine) as writer:
            create_event(
                writer,
                EventInput(
                    start=datetime(2026, 9, 1, tzinfo=UTC),
                    payload={"type": "note", "description": "synthetic concurrent diary"},
                ),
                actor="test",
            )
            future = pool.submit(enroll)
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
        with pytest.raises(AccountEnrollmentRequired):
            future.result(timeout=5)
    assert db.get(AppState, BINDING_KEY) is None


def test_file_only_probe_rejects_existing_other_owner_before_fetch(tmp_path, monkeypatch):
    from garmin_ai import cli

    settings = Settings(data_dir=tmp_path / "data", token_dir=tmp_path / "tokens")
    archive = LocalArchive(settings.data_dir / "raw")
    archive.put_json({"synthetic_owner_a": True})
    report = settings.data_dir / "coverage-report.json"
    original = json.dumps({"account_fingerprint": A})
    report.write_text(original)
    before = set(archive.root.rglob("*"))
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli.GarminReader, "restore", lambda _: SimpleNamespace(account_fingerprint=lambda: B)
    )
    monkeypatch.setattr(
        cli, "probe", lambda *args, **kwargs: pytest.fail("must not fetch another owner")
    )
    monkeypatch.setattr("sys.argv", ["garmin-ai", "probe"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert report.read_text() == original and set(archive.root.rglob("*")) == before


def test_file_probe_allows_matching_provenance(tmp_path):
    from garmin_ai.accounts import verify_file_probe

    archive = LocalArchive(tmp_path / "raw")
    archive.put_json({"synthetic": True})
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"account_fingerprint": A}))
    assert verify_file_probe(archive.root, report, A) is None
    report.write_text("{}")
    with pytest.raises(AccountEnrollmentRequired):
        verify_file_probe(archive.root, report, A)
