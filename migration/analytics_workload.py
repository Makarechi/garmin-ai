"""Measure long analytical queries against deterministic synthetic diary and health data."""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from threading import Event

from sqlalchemy import insert, select

from garmin_ai.analytics import compare_periods, lagged_association, migraine_comparison
from garmin_ai.config import Settings
from garmin_ai.db import make_engine, transaction
from garmin_ai.events import EventInput, create_event
from garmin_ai.generic_analytics import AnalysisSpec, execute_analysis
from garmin_ai.models import HealthDay, MetricDefinition
from garmin_ai.scenario_packs import PackSelection, configure_scenario_pack, ensure_scenario_packs
from garmin_ai.tracker_forms import (
    FormSubmission,
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    form_for_action,
    preview_tracker,
    submit_form,
)

START = date(2024, 10, 1)
END = START + timedelta(days=729)


def utc(day):
    return datetime.combine(day, clock_time.min, tzinfo=UTC)


def seed(session):
    days = [
        {
            "day": START + timedelta(days=offset),
            "sleep_score": None if offset % 11 == 0 else 55 + offset % 37,
            "steps": 3000 + (offset * 123) % 5000,
        }
        for offset in range(730)
    ]
    session.execute(insert(HealthDay), days)
    pack = ensure_scenario_packs(session)["migraine"]
    configure_scenario_pack(
        session,
        "migraine",
        PackSelection(
            revision=pack.revision,
            tracking_enabled=True,
            collection_enabled=pack.collection_enabled,
            reminders_enabled=False,
            visible=True,
            llm_enabled=pack.llm_enabled,
        ),
    )
    draft = TrackerSetupDraft(
        key="synthetic_focus",
        name="Synthetic focus",
        locale="en",
        topology="point",
        fields=[
            TrackerFieldDraft(key="quality", label="Quality", kind="scale", minimum=1, maximum=5)
        ],
    )
    preview = preview_tracker(session, draft)
    created = confirm_tracker(
        session,
        TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
        actor="test",
    )
    action = created["action"]["id"]
    form = form_for_action(session, action)
    for offset in range(0, 365, 3):
        at = utc(START + timedelta(days=offset)) + timedelta(hours=12)
        submit_form(
            session,
            action,
            FormSubmission(
                action_id=action,
                schema_hash=form.schema_hash,
                start=at,
                timezone="UTC",
                values={"quality": offset % 5 + 1},
                units={"quality": "score_1-5"},
            ),
            actor="test",
            idempotency_key=f"synthetic-focus:{offset}",
        )
    for offset in range(365):
        day = START + timedelta(days=offset)
        if offset % 15 == 2:
            event = EventInput(
                start=utc(day) + timedelta(hours=13),
                end=utc(day) + timedelta(hours=16),
                timezone="UTC",
                payload={"type": "migraine"},
            )
        else:
            event = EventInput(
                start=utc(day),
                end=utc(day + timedelta(days=1)),
                timezone="UTC",
                payload={"type": "headache_observation", "headache": "no", "migraine": "no"},
            )
        create_event(session, event, actor="test", idempotency_key=f"synthetic-migraine:{offset}")
    return session.scalar(select(MetricDefinition.key).where(MetricDefinition.key.like("user.%")))


def run(repetitions):
    settings = Settings(_env_file=None)
    engine = make_engine(settings)
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The analytical workload requires a dedicated synthetic test database")
    try:
        with transaction(engine) as session:
            metric_key = seed(session)
        spec = AnalysisSpec(
            operation="aggregate_metric",
            metric_key=metric_key,
            start=utc(START),
            end=utc(START + timedelta(days=365)),
            method="distribution",
            knowledge_cutoff=datetime.now(UTC),
        )
        start = time.monotonic()
        for _ in range(repetitions):
            with transaction(engine) as session:
                periods = compare_periods(
                    session,
                    "sleep_score",
                    START,
                    START + timedelta(days=364),
                    START + timedelta(days=365),
                    END,
                )
                lags = lagged_association(
                    session, "steps", "sleep_score", START, END, list(range(-7, 8))
                )
                migraine = migraine_comparison(
                    session, "sleep_score", START, START + timedelta(days=364), "UTC"
                )
                generic = execute_analysis(session, spec)
        elapsed = time.monotonic() - start
        if (
            not periods["ci95"]
            or len(lags["results"]) != 15
            or not migraine["matched_pairs"]
            or sum(generic["value"].values()) != 122
        ):
            raise RuntimeError("The analytical workload did not return the expected evidence")
        return {
            "health_days": 730,
            "custom_entries": 122,
            "headache_days": 365,
            "repetitions": repetitions,
            "period_interval": periods["ci95"],
            "lag_pairs": [row["n"] for row in lags["results"]],
            "matched_controls": migraine["matched_pairs"],
            "generic_distribution": generic["value"],
            "queries_seconds": round(elapsed, 3),
        }
    finally:
        engine.dispose()


def run_http(repetitions, base):
    from web_chat_workload import call, controls

    engine = make_engine(Settings(_env_file=None))
    if not (engine.url.database or "").endswith("_test") or engine.url.query:
        raise ValueError("The analytical workload requires a dedicated synthetic test database")
    seed_started = time.monotonic()
    try:
        with transaction(engine) as session:
            metric = seed(session)
    finally:
        engine.dispose()
    seed_seconds = round(time.monotonic() - seed_started, 3)
    midpoint = START + timedelta(days=365)
    cutoff = datetime.now(UTC).isoformat()
    cases = [
        (
            "analysis_compare_periods",
            {
                "metric": "sleep_score",
                "a_start": START.isoformat(),
                "a_end": (midpoint - timedelta(days=1)).isoformat(),
                "b_start": midpoint.isoformat(),
                "b_end": END.isoformat(),
            },
        ),
        (
            "analysis_lagged_association",
            {
                "metric_a": "steps",
                "metric_b": "sleep_score",
                "start": START.isoformat(),
                "end": END.isoformat(),
                "lags": list(range(-7, 8)),
            },
        ),
        (
            "analysis_migraine_windows",
            {
                "metric": "sleep_score",
                "start": START.isoformat(),
                "end": (midpoint - timedelta(days=1)).isoformat(),
                "timezone": "UTC",
            },
        ),
        (
            "generic_analysis",
            {
                "spec": {
                    "operation": "aggregate_metric",
                    "metric_key": metric,
                    "start": utc(START).isoformat(),
                    "end": utc(midpoint).isoformat(),
                    "method": "distribution",
                    "knowledge_cutoff": cutoff,
                }
            },
        ),
    ]
    token = os.environ["GA_API_KEY"]
    started = time.monotonic()
    finished = Event()
    timings = {name: [] for name, _ in cases}
    previous = None
    with ThreadPoolExecutor(max_workers=1) as observer:
        probes = observer.submit(controls, base, token, finished)
        try:
            for _ in range(repetitions):
                responses = []
                for name, arguments in cases:
                    value, latency = call(
                        base,
                        f"/tools/{name}",
                        token=token,
                        body={"arguments": arguments},
                        timeout=60,
                    )
                    responses.append(value)
                    timings[name].append(latency)
                periods, lags, migraine, generic = responses
                result = {
                    "period_interval": periods["ci95"],
                    "period_difference": periods["difference"],
                    "lag_pairs": [row["n"] for row in lags["results"]],
                    "matched_controls": migraine["matched_pairs"],
                    "generic_distribution": generic["value"],
                }
                if previous is not None and result != previous:
                    raise RuntimeError("Repeated analytical queries changed the synthetic result")
                previous = result
        finally:
            finished.set()
        overlap = probes.result()
    if (
        not result["period_interval"]
        or len(result["lag_pairs"]) != 15
        or not result["matched_controls"]
        or sum(result["generic_distribution"].values()) != 122
    ):
        raise RuntimeError("The HTTP analytical workload did not return the expected evidence")
    return {
        "health_days": 730,
        "custom_entries": 122,
        "headache_days": 365,
        "repetitions": repetitions,
        "result": result,
        "seed_seconds": seed_seconds,
        "queries_seconds": round(time.monotonic() - started, 3),
        "tool_latency_ms": timings,
        "overlap": overlap,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repetitions", type=int, choices=(1, 10))
    parser.add_argument("--http-base")
    options = parser.parse_args()
    outcome = (
        run_http(options.repetitions, options.http_base)
        if options.http_base
        else run(options.repetitions)
    )
    print(json.dumps(outcome, default=str))
