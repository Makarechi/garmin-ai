"""Compare permitted and unsupported categorical evidence through the ordinary tracker route."""

from unittest.mock import patch

import check_tracker_extraction_http as baseline
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.models import EventDefinition, EventDefinitionVersion
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerFieldDraft,
    TrackerSetupDraft,
    confirm_tracker,
    preview_tracker,
)

EXTRACTION = baseline.extraction
TOKEN = {"Authorization": "Bearer synthetic-tracker-nl-fallback-api-admin"}


def extraction(prompt):
    result = EXTRACTION(prompt)
    if prompt["text"].endswith("alto"):
        result["fields"][0]["value"] = "high"
    return result


def exercise(engine, python, native, python_calls, native_calls):
    with Session(engine) as session, session.begin():
        draft = TrackerSetupDraft(
            key="synthetic_category",
            name="Synthetic category",
            locale="en",
            fields=[
                TrackerFieldDraft(
                    key="level", label="Level", kind="choice", options=["low", "high", "высокий"]
                )
            ],
        )
        preview = preview_tracker(session, draft)
        confirm_tracker(
            session,
            TrackerConfirmation(draft=draft, confirmation_token=preview["confirmation_token"]),
            actor="synthetic-fixture",
        )
        version = session.scalar(
            select(EventDefinitionVersion.id)
            .join(EventDefinition)
            .where(EventDefinition.key == "user.synthetic_category")
        )
        assert version

    for index, (sentence, value) in enumerate(
        [
            ("Synthetic now high", "high"),
            ("Синтетический сейчас высокий", "высокий"),
            ("Synthetic now alto", None),
            ("Synthetic now disallowed", None),
        ]
    ):
        results = []
        for role, server in (("python", python), ("rust", native)):
            body = {
                "text": sentence,
                "operation_id": f"synthetic-categories-{role}-{index}",
                "selected_definition_version_id": str(version),
            }
            baseline.pinned(engine, body)
            response = server.post("/natural-language/trackers", json=body, headers=TOKEN)
            expected_status = 422 if sentence.endswith("disallowed") else 200
            assert response.status_code == expected_status, (role, sentence, response.text)
            result = response.json()
            if expected_status == 422:
                assert result["errors"][0]["code"] == "enum"
                results.append(result)
                continue
            assert result["written"] == (value is not None), (role, result)
            if value is not None:
                event = baseline.logical_event(engine, result["event_id"])
                assert event["payload"]["level"] == value and event["original_text"] == sentence
                assert (
                    server.post("/natural-language/trackers", json=body, headers=TOKEN).json()
                    == result
                )
            results.append(baseline.clean_response(result))
        assert results[0] == results[1], results
    assert len(python_calls) == len(native_calls) == 4
    print(
        "Both runtimes stored only matching allowed categories with original bilingual evidence and skipped ungrounded translation and out-of-contract values."
    )


if __name__ == "__main__":
    with (
        patch.object(baseline, "extraction", extraction),
        patch.object(baseline, "exercise", exercise),
    ):
        baseline.main()
