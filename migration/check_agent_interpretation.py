"""Record the pinned model interpretation contract and diary draft validation."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from garmin_ai.agent import Interpretation

TARGET = str(UUID(int=3101))
QUESTION = str(UUID(int=3102))
AT = "2026-11-17T12:00:00+00:00"
PARENT = {"start": AT, "payload": {"type": "migraine", "severity": 6}}
CHILD = {
    "start": AT,
    "payload": {"type": "medication", "name": "synthetic intake", "dose": 5, "unit": "mg"},
}
LINK = {"child_index": 1, "parent_index": 0}


def capture(output):
    cases = [
        ("question", {"intent": "question", "confidence": 0.7}),
        (
            "regional_language",
            {"intent": "safety", "confidence": 1, "response_language": "ZH-hans-cn"},
        ),
        (
            "private_language",
            {"intent": "clarify", "confidence": 0.4, "response_language": "X-FOX"},
        ),
        (
            "malformed_language",
            {"intent": "clarify", "confidence": 0.4, "response_language": "en-u"},
        ),
        (
            "duplicate_language",
            {"intent": "question", "confidence": 0.4, "response_language": "de-1901-1901"},
        ),
        ("missing_events", {"intent": "log", "confidence": 0.9}),
        ("missing_update_target", {"intent": "update", "events": [PARENT], "confidence": 0.9}),
        ("missing_close_target", {"intent": "close", "events": [PARENT], "confidence": 0.9}),
        (
            "valid_update",
            {
                "intent": "update",
                "events": [PARENT],
                "target_event_id": TARGET,
                "target_question_id": QUESTION,
                "question_context_ids": [QUESTION],
                "confidence": 0.9,
                "changed_fields": ["severity"],
            },
        ),
        (
            "linked_batch",
            {"intent": "log", "events": [PARENT, CHILD], "draft_links": [LINK], "confidence": 0.9},
        ),
        (
            "duplicate_link",
            {
                "intent": "log",
                "events": [PARENT, CHILD],
                "draft_links": [LINK, LINK],
                "confidence": 0.9,
            },
        ),
        (
            "link_outside",
            {
                "intent": "log",
                "events": [PARENT, CHILD],
                "draft_links": [{**LINK, "parent_index": 2}],
                "confidence": 0.9,
            },
        ),
        (
            "inverted_link",
            {"intent": "log", "events": [CHILD, PARENT], "draft_links": [LINK], "confidence": 0.9},
        ),
        (
            "non_log_link",
            {
                "intent": "question",
                "events": [PARENT, CHILD],
                "draft_links": [LINK],
                "confidence": 0.9,
            },
        ),
        (
            "unconfirmed_parent",
            {
                "intent": "log",
                "events": [{**PARENT, "status": "needs_confirmation"}, CHILD],
                "draft_links": [LINK],
                "confidence": 0.9,
            },
        ),
        (
            "existing_relation",
            {
                "intent": "log",
                "events": [
                    PARENT,
                    {**CHILD, "payload": {**CHILD["payload"], "reason_event_id": TARGET}},
                ],
                "draft_links": [LINK],
                "confidence": 0.9,
            },
        ),
        ("unrecognized_field", {"intent": "question", "confidence": 0.8, "provider_data": {}}),
        ("invalid_uuid", {"intent": "question", "confidence": 0.8, "target_question_id": "bad"}),
        (
            "invalid_timezone",
            {"intent": "log", "confidence": 0.8, "events": [{**PARENT, "timezone": "Asdf/User"}]},
        ),
        ("bad_confidence", {"intent": "question", "confidence": 1.01}),
        (
            "explicit_clarification",
            {
                "intent": "clarify",
                "clarification": "synthetic question 🧪",
                "confidence": 0.4,
                "response_language": "i-KLINGON",
            },
        ),
        (
            "unusable_relation_index",
            {
                "intent": "log",
                "events": [PARENT, CHILD],
                "draft_links": [{**LINK, "parent_index": False}],
                "confidence": 0.9,
            },
        ),
    ]
    captured = []
    for name, value in cases:
        try:
            result = Interpretation.model_validate(value)
            body = result.model_dump(mode="json")
            body["events"] = [
                {
                    "kind": event["payload"]["type"],
                    "source": event["source"],
                    "status": event["status"],
                    "timezone": event["timezone"],
                }
                for event in body["events"]
            ]
        except ValueError:
            body = None
        captured.append({"name": name, "input": value, "result": body})
    output.write_text(json.dumps(captured, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    capture(parser.parse_args().output)
