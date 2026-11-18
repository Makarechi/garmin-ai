"""Record the pinned model-visible diary context on disposable PostgreSQL."""

import argparse
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from capture_reference import isolated_engine
from check_model_diary_policy import END, seed
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.agent import context_for
from garmin_ai.models import Event, ModuleConfig, Person

LATER = END + timedelta(days=2)


def add_history(engine):
    with Session(engine) as session, session.begin():
        person = session.scalar(select(Person.id))
        session.add(
            ModuleConfig(
                owner_id=person, pack_key="migraine", tracking_enabled=True, llm_enabled=True
            )
        )
        for index in range(14):
            session.add(
                Event(
                    id=UUID(int=100 + index),
                    kind="note",
                    start=LATER - timedelta(minutes=index),
                    source="manual",
                    timezone="UTC",
                    original_text="synthetic quote",
                    payload={"synthetic": index},
                )
            )
        for index, start in enumerate([END - timedelta(days=30), LATER + timedelta(days=1)]):
            session.add(
                Event(
                    id=UUID(int=120 + index),
                    kind="migraine",
                    start=start,
                    topology="open_interval",
                    source="manual",
                    timezone="UTC",
                    original_text="synthetic quote",
                    payload={"synthetic": "episode"},
                )
            )


def capture(output):
    engine = isolated_engine()
    try:
        seed(engine)
        add_history(engine)
        cases = []
        for now, model, channel in [
            (END, "model:primary", "telegram:allowed"),
            (END, "model:primary", "telegram:schema"),
            (END, "model:other", "telegram:allowed"),
            (LATER, "model:primary", "telegram:allowed"),
        ]:
            with Session(
                engine,
                info={
                    "model_provider_instance_id": model,
                    "channel_destination_instance_id": channel,
                },
            ) as session:
                context = context_for(session, now, maintain=False)
                rows = context["recent_events"]
                for row in rows:
                    row.pop("original_text", None)
                assert str(UUID(int=120)) in [row["id"] for row in rows]
                assert str(UUID(int=121)) not in [row["id"] for row in rows]
                cases.append(
                    dict(
                        now=now.isoformat(),
                        model=model,
                        channel=channel,
                        rows=rows,
                        truncated=context["history_truncated"],
                    )
                )
        assert not cases[0]["truncated"]
        assert cases[3]["truncated"]
        output.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
