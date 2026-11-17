"""Capture the committed and interrupted states of a synthetic offline replay."""

from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from garmin_ai.accounts import bind_account, profile_fingerprint
from garmin_ai.archive import LocalArchive
from garmin_ai.config import Settings
from garmin_ai.db import transaction
from garmin_ai.ingest import ingest
from garmin_ai.models import HealthDay, Job, SourcePayload
from garmin_ai.replay import replay_status, run_replay, schedule_replay
from garmin_ai.scenario_packs import PackSelection, configure_scenario_pack, ensure_scenario_packs


class Interrupted(RuntimeError):
    """Synthetic failure between writing archive bytes and committing the database."""


def capture_replay(engine, directory, case):
    day_key = date.fromisoformat(case["day"])
    fetch_time = datetime.fromisoformat(case["first_fetch"])
    timezone = case["timezone"]
    archive = LocalArchive(directory)
    aliases = {}

    def fetched(session, steps, offset):
        result = ingest(
            session,
            archive,
            "daily",
            day_key.isoformat(),
            {"totalSteps": steps},
            timezone,
            fetched_at=fetch_time + timedelta(seconds=offset),
        )
        return result

    def label(reference):
        return aliases.setdefault(reference, f"raw:{len(aliases) + 1}")

    def snapshot():
        with transaction(engine) as session:
            stored = [
                {
                    "id": label(str(row.id)),
                    "endpoint": row.endpoint,
                    "source": row.source,
                    "source_key": row.source_key,
                    "payload_hash": row.payload_hash,
                    "archive_key": row.archive_key,
                    "payload": row.payload,
                    "parser_version": row.parser_version,
                    "status": row.status,
                    "fetched_at": row.fetched_at.isoformat(),
                }
                for row in session.scalars(
                    select(SourcePayload).order_by(SourcePayload.payload_hash)
                )
            ]
            day = session.get(HealthDay, day_key)
            return {
                "sources": stored,
                "archive_keys": sorted(
                    str(path.relative_to(directory)) for path in directory.glob("*/*.json")
                ),
                "daily_steps": day.steps if day is not None else None,
                "database_source_count": session.scalar(
                    select(func.count()).select_from(SourcePayload)
                ),
            }

    with transaction(engine) as session:
        bind_account(
            session, profile_fingerprint({"profileId": 12345}), confirm_existing_owner=True
        )
        packs = ensure_scenario_packs(session)
        for name in ("training", "wellbeing"):
            current = packs[name]
            configure_scenario_pack(
                session,
                name,
                PackSelection(
                    revision=current.revision,
                    tracking_enabled=current.tracking_enabled,
                    collection_enabled=True,
                    reminders_enabled=current.reminders_enabled,
                    visible=current.visible,
                    llm_enabled=current.llm_enabled,
                    outcome_goal=current.outcome_goal,
                ),
            )

    try:
        with transaction(engine) as session:
            fetched(session, case["interrupted_steps"], 0)
            raise Interrupted("synthetic interruption before commit")
    except Interrupted:
        halfway = snapshot()

    engine.dispose()
    sequence = []
    for offset, steps in enumerate(case["committed_steps"], start=1):
        with transaction(engine) as session:
            result = fetched(session, steps, offset)
            sequence.append(
                {
                    "steps": steps,
                    "status": result["status"],
                    "source_ref": label(result["source_ref"]),
                    "current_steps": session.get(HealthDay, day_key).steps,
                }
            )
    before_replay = snapshot()
    with transaction(engine) as session:
        for source in session.scalars(select(SourcePayload)):
            source.parser_version = 0
        schedule_replay(session, fetch_time + timedelta(minutes=1))
        payloads = [
            dict(job.payload)
            for job in session.scalars(select(Job).where(Job.kind == "raw_replay"))
        ]

    settings = Settings(_env_file=None, timezone=timezone)
    outcomes = []
    for payload in sorted(payloads, key=lambda job: aliases[job["raw_ref"]], reverse=True):
        result = run_replay(engine, archive, settings, payload)
        outcomes.append({"source_ref": aliases[payload["raw_ref"]], "status": result["status"]})
    with transaction(engine) as session:
        status = replay_status(session)
    return {
        "after_interrupted_transaction": halfway,
        "requests": sequence,
        "before_replay": before_replay,
        "after_replay": snapshot(),
        "replay_outcomes": outcomes,
        "replay_state_outcomes": status["outcomes"],
    }
