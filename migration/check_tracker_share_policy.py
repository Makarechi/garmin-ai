"""Compare consent decisions with pinned Python, then exercise live delivery locks."""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.models import AppState, EventDefinition, EventDefinitionVersion, Person
from garmin_ai.share_policy import TrackerShareConsent, version_sharing_allowed

PRIVATE = UUID(int=1)
SENSITIVE = UUID(int=2)
DEFINITION = UUID(int=44)


def seed(engine):
    with Session(engine) as session, session.begin():
        session.add(
            EventDefinition(
                id=DEFINITION,
                owner_id=session.scalar(select(Person.id)),
                namespace="user",
                key="user.synthetic",
                status="retired",
                current_version=2,
            )
        )
        session.flush()
        for index, (identity, privacy) in enumerate(
            [(PRIVATE, "private"), (SENSITIVE, "sensitive")], 1
        ):
            session.add(
                EventDefinitionVersion(
                    id=identity,
                    definition_id=DEFINITION,
                    version=index,
                    schema={},
                    schema_hash="0" * 64,
                    topology="point",
                    field_metadata={},
                    labels={"en": "Synthetic tracker"},
                    privacy=privacy,
                    allowed_operations=["query"],
                )
            )
        now = datetime.now(UTC)
        for kind, instance, categories, granted in [
            ("model", "model:gemini:primary", {"facts"}, now - timedelta(seconds=5)),
            ("model", "model:openai:voice", {"original_text"}, now - timedelta(seconds=5)),
            ("model", "model:future", {"schema", "facts"}, now + timedelta(days=1)),
            ("channel", "telegram:primary", {"schema", "facts"}, now - timedelta(seconds=5)),
            ("channel", "telegram:schema", {"schema"}, now - timedelta(seconds=5)),
        ]:
            consent = TrackerShareConsent(
                definition_id=DEFINITION,
                destination_kind=kind,
                destination_instance_id=instance,
                categories=categories,
                granted_at=granted,
            )
            session.add(
                AppState(
                    key=f"tracker-consent:{DEFINITION}:{kind}:{instance}",
                    value=consent.model_dump(mode="json"),
                )
            )


def run(binary):
    engine = isolated_engine()
    try:
        seed(engine)
        cases = []
        with Session(engine) as session:
            for version in (PRIVATE, SENSITIVE, UUID(int=99)):
                for kind, instance, categories in [
                    ("model", "model:gemini:primary", {"facts"}),
                    ("model", "model:gemini:primary", {"schema", "facts"}),
                    ("model", "model:openai:voice", {"original_text"}),
                    ("model", "model:openai:voice", {"original_text", "facts"}),
                    ("model", "model:future", {"schema"}),
                    ("channel", "telegram:primary", {"schema", "facts"}),
                    ("channel", "telegram:primary", {"original_text"}),
                    ("channel", "telegram:schema", {"schema", "facts"}),
                    ("channel", "telegram:other", {"schema"}),
                    ("model", "telegram:primary", {"schema", "facts"}),
                ]:
                    cases.append(
                        {
                            "version": str(version),
                            "destination_kind": kind,
                            "instance": instance,
                            "categories": sorted(categories),
                            "allowed": version_sharing_allowed(
                                session,
                                version,
                                destination_kind=kind,
                                destination_instance_id=instance,
                                categories=categories,
                            ),
                        }
                    )
        assert any(case["allowed"] for case in cases)
        assert any(not case["allowed"] for case in cases)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "share-policy.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            subprocess.run(
                [binary, "--ignored"],
                check=True,
                env={**os.environ, "GA_TEST_TRACKER_SHARE_POLICY": str(path)},
            )
        print(f"Matched {len(cases)} pinned tracker-sharing decisions and verified delivery locks")
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    run(parser.parse_args().binary.resolve(strict=True))
