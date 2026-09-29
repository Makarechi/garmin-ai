import ast
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from garmin_ai.definitions import (
    CustomEntryInput,
    DefinitionSpec,
    activate_definition,
    create_custom_event,
    create_definition_draft,
)
from garmin_ai.jobs import claim, enqueue, finish, retire_disabled_source_jobs
from garmin_ai.models import (
    AppState,
    Audit,
    Base,
    Event,
    EventDefinition,
    EventDefinitionVersion,
    Job,
)
from garmin_ai.operations import (
    COMPATIBLE_EXPORT_REVISIONS,
    REVISION,
    export_database,
    restore_database,
)
from garmin_ai.share_policy import (
    TrackerShareConsent,
    grant_tracker_share,
    version_sharing_allowed,
)

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "tests" / "universal_acceptance_manifest.json"


def _test_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _migration_revisions():
    revisions = {}
    for path in (ROOT / "src" / "garmin_ai" / "migrations" / "versions").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            elif isinstance(node, ast.AnnAssign):
                target = node.target
            else:
                continue
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                values[target.id] = ast.literal_eval(node.value)
        revisions[values["revision"]] = values.get("down_revision")
    return revisions


def test_acceptance_manifest_has_all_scenarios_and_existing_evidence():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    scenarios = manifest["scenarios"]
    assert [row["id"] for row in scenarios] == [f"UT-{number:02d}" for number in range(1, 54)]
    assert manifest["environment"] == "synthetic-postgresql-timescaledb"
    assert manifest["live_services_used"] is False
    for scenario in scenarios:
        assert scenario["intent"].strip(), scenario["id"]
        assert scenario["level"] in {"unit", "service-db", "entry-point", "migration"}, scenario[
            "id"
        ]
        assert scenario["assertions"] and all(
            isinstance(assertion, str) and len(assertion.strip()) >= 20
            for assertion in scenario["assertions"]
        ), scenario["id"]
        assert scenario["limitations"].strip(), scenario["id"]
        assert scenario["evidence"], scenario["id"]
        for nodeid in scenario["evidence"]:
            relative, function = nodeid.split("::", 1)
            path = ROOT / relative
            assert path.is_file(), nodeid
            assert function in _test_functions(path), nodeid


def test_reference_channel_claim_has_actual_entrypoint_evidence():
    scenarios = json.loads(MANIFEST.read_text(encoding="utf-8"))["scenarios"]
    evidence = {row["id"]: set(row["evidence"]) for row in scenarios}
    assert {
        "tests/test_hf09_entrypoint_parity.py::test_create_retries_have_one_fact_and_audit_via_actual_ingress",
        "tests/test_hf09_entrypoint_parity.py::test_edit_uses_pinned_revision_via_actual_ingress",
        "tests/test_hf09_entrypoint_parity.py::test_open_interval_close_via_actual_entrypoint",
        "tests/test_hf09_entrypoint_parity.py::test_invalid_value_clarifies_without_writing_a_fact",
    } <= evidence["UT-39"]
    assert (
        "tests/test_hf09_entrypoint_parity.py::test_reference_capabilities_fall_back_and_stale_revision_fails_after_restart"
        in evidence["UT-40"]
    )


def test_portable_export_revision_is_the_only_migration_head():
    revisions = _migration_revisions()
    parents = {
        parent
        for value in revisions.values()
        for parent in (value if isinstance(value, tuple) else (value,))
        if parent is not None
    }
    heads = set(revisions) - parents
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert heads == {REVISION}
    assert manifest["database_revision"] == REVISION
    assert REVISION in COMPATIBLE_EXPORT_REVISIONS


def test_custom_acceptance_examples_are_not_product_types():
    forbidden = {"focus_session", "stretching", "concentration_tracker"}
    for path in (ROOT / "src" / "garmin_ai").rglob("*"):
        if path.suffix not in {".py", ".html", ".js"}:
            continue
        content = path.read_text(encoding="utf-8").lower()
        assert not (forbidden & set(content.replace("-", "_").split())), path


def test_unreleased_whatsapp_is_not_a_runtime_dependency_or_claim():
    paths = [ROOT / "pyproject.toml", ROOT / "README.md"]
    paths.extend((ROOT / "src" / "garmin_ai").rglob("*.py"))
    assert all("whatsapp" not in path.read_text(encoding="utf-8").lower() for path in paths)


def test_release_metadata_records_sha_revision_and_verification_boundary():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_metadata.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert len(report["git_sha"]) == 40
    assert report["database_revision"] == REVISION
    assert report["live_services_used"] is False
    assert report["test_environment"] == "disposable synthetic PostgreSQL/TimescaleDB"
    assert report["commands"]
    assert report["profile"] == "full"

    core = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_metadata.py"), "--profile", "core-only"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    core_report = json.loads(core.stdout)
    assert core_report["git_sha"] == report["git_sha"]
    assert core_report["profile"] == "core-only"
    assert any("tests/test_core_only_flow.py" in command for command in core_report["commands"])
    assert any(
        "--junitxml=test-results/core-only.xml" in command for command in core_report["commands"]
    )
    assert all("--extra full" not in command for command in core_report["commands"])


def test_upgrade_restart_queue_and_export_restore_roundtrip(db, db_engine, tmp_path):
    assert db.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
    now = datetime.now(UTC)
    spec = DefinitionSpec(
        key="user.release_roundtrip",
        labels={"en": "Synthetic release check"},
        topology="point",
        privacy="sensitive",
        schema={
            "type": "object",
            "properties": {"note": {"type": "string", "maxLength": 40}},
            "required": ["note"],
            "additionalProperties": False,
        },
        fields={
            "note": {
                "id": "user.release_roundtrip.note",
                "labels": {"en": "Note"},
                "semantic": "text",
            }
        },
    )
    definition = create_definition_draft(db, spec, actor="release-test", authorized=True)
    version = activate_definition(
        db, definition.id, definition.revision, actor="release-test", authorized=True
    )
    event = create_custom_event(
        db,
        CustomEntryInput(
            definition_key=spec.key,
            start=now,
            timezone="UTC",
            status="needs_confirmation",
            values={"note": "synthetic"},
        ),
        actor="release-test",
        idempotency_key="release:synthetic-entry",
    )
    grant_tracker_share(
        db,
        TrackerShareConsent(
            definition_id=definition.id,
            destination_kind="channel",
            destination_instance_id="telegram:primary",
            categories={"schema", "facts"},
            granted_at=now - timedelta(minutes=1),
        ),
        authorized=True,
    )
    definition_id, version_id, event_id = definition.id, version.id, event.id
    job_id = enqueue(db, "sync", {"day": "2026-09-21"}, "release:restart", now)
    pending_id = enqueue(db, "agent_proactive", {}, "release:pending", now + timedelta(hours=1))
    db.commit()
    crashed = claim(db, now=now, lease_seconds=1)
    stale_token = crashed.lease_token
    db.commit()

    with Session(db_engine) as restarted:
        recovered = claim(restarted, now=now + timedelta(seconds=2))
        assert recovered.id == job_id
        finish(restarted, recovered.id, recovered.lease_token)
        restarted.commit()

    db.expire_all()
    with pytest.raises(ValueError, match="lease expired|different worker"):
        finish(db, job_id, stale_token)
    assert db.get(Job, job_id).status == "done"
    db.rollback()

    exported = tmp_path / "universal-release.jsonl.gz"
    counts = export_database(db_engine, exported)
    names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    assert restore_database(db_engine, exported) == counts
    with Session(db_engine) as restored:
        row = restored.scalar(select(Job).where(Job.id == job_id))
        assert row.status == "done"
        assert restored.get(Job, pending_id).status == "pending"
        assert restored.get(EventDefinition, definition_id).key == spec.key
        assert restored.get(EventDefinitionVersion, version_id).privacy == "sensitive"
        restored_event = restored.get(Event, event_id)
        assert restored_event.definition_version_id == version_id
        assert restored_event.status == "needs_confirmation"
        assert restored_event.payload["note"] == "synthetic"
        assert restored.scalar(select(Audit).where(Audit.event_id == event_id)).action == "create"
        assert restored.get(AppState, f"tracker-consent:{definition_id}:channel:telegram:primary")
        assert version_sharing_allowed(
            restored,
            version_id,
            destination_kind="channel",
            destination_instance_id="telegram:primary",
            categories={"schema", "facts"},
        )
        assert not version_sharing_allowed(
            restored,
            version_id,
            destination_kind="channel",
            destination_instance_id="telegram:secondary",
            categories={"schema", "facts"},
        )


def test_disabling_garmin_retires_source_jobs_and_unblocks_agent_work(db):
    now = datetime.now(UTC)
    source_id = enqueue(db, "garmin_endpoint", {"endpoint": "stress"}, "source:stress", now)
    proactive_id = enqueue(db, "agent_proactive", {}, "agent:proactive", now)

    assert retire_disabled_source_jobs(db, now) == 1
    assert db.get(Job, source_id).status == "cancelled"
    assert db.get(Job, source_id).last_error == "SourceDisabled"
    claimed = claim(db, kinds=["agent_proactive"], now=now)
    assert claimed.id == proactive_id
