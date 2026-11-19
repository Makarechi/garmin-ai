"""Capture the frozen persisted form-safety statuses across repeated processing."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from capture_reference import isolated_engine
from sqlalchemy.orm import Session

from garmin_ai.diary_forms import check_form_safety
from garmin_ai.llm import ProviderUnavailable
from garmin_ai.models import Job


class SyntheticProvider:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def structured(self, instruction, prompt, schema):
        assert prompt == "Synthetic persisted safety classification"
        self.calls += 1
        if self.outcome == "unavailable":
            raise ProviderUnavailable("Synthetic safety provider unavailable")
        return schema(urgent=self.outcome == "urgent")


def capture(engine):
    rows = []
    for outcome in ["checked", "urgent", "unavailable"]:
        for initial in [None, False, 0, "unknown", "CHECKED", "checked", "urgent", "unavailable"]:
            update = 73001 + len(rows)
            original = {"retained": {"synthetic": True}, "form_safety": initial}
            with Session(engine) as session:
                session.add(
                    Job(
                        kind="telegram.message",
                        dedup_key=f"telegram:{update}",
                        payload=original,
                        run_at=datetime.now(UTC),
                    )
                )
                session.commit()
            provider = SyntheticProvider(outcome)
            statuses = []
            for _ in range(3):
                with Session(engine) as session:
                    statuses.append(
                        check_form_safety(
                            session, provider, "Synthetic persisted safety classification", update
                        )
                    )
            with Session(engine) as session:
                job = session.query(Job).filter_by(dedup_key=f"telegram:{update}").one()
                assert job.payload["retained"] == original["retained"]
                rows.append(
                    {
                        "initial": initial,
                        "outcome": outcome,
                        "statuses": statuses,
                        "calls": provider.calls,
                        "payload": job.payload,
                    }
                )
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        rows = capture(engine)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        print(
            f"Captured {len(rows)} persisted safety contracts over {len(rows) * 3} repeated calls"
        )
    finally:
        engine.dispose()
