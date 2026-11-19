"""Capture frozen Telegram replies to selected trackers predating guided forms."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

import capture_tracker_chat_entries as entries
from capture_reference import isolated_engine
from sqlalchemy import select
from sqlalchemy.orm import Session

from garmin_ai.llm import ProviderUnavailable
from garmin_ai.models import AppState, Event, EventDefinition, EventDefinitionVersion, TrackerConfig
from garmin_ai.natural_language import NaturalLanguageRequest


def seed(engine):
    entries.seed(engine)
    with Session(engine) as session:
        for definition in session.scalars(
            select(EventDefinition).where(EventDefinition.key.startswith("user.synthetic_chat_"))
        ):
            session.add(TrackerConfig(owner_id=definition.owner_id, definition_id=definition.id))
        session.commit()


def cases():
    rows = []

    def answer(text, **options):
        rows.append({"message": {"text": text}, **options})

    for name in ["text", "integer", "number", "boolean", "choice", "json"]:
        answer("Synthetic old pending entry", legacy_tracker=name)
        answer("2026-11-18 01:00Z")
        answer({"text": "Synthetic legacy value", "integer": "3", "number": "0.5",
                "boolean": "yes", "choice": "A", "json": '["legacy"]'}[name])
    answer("Synthetic old sensitive entry", legacy_tracker="sensitive", grant=["schema"])
    answer("2026-11-18 02:00Z")
    answer("Synthetic legacy sensitive")
    answer("Synthetic denied entry", legacy_tracker="sensitive", revoke=True)
    answer("Synthetic unavailable entry", legacy_tracker="text", tracker_status={"text": "retired"})
    answer("/cancel")
    answer("/unrecognized", legacy_tracker="text", tracker_status={"text": "active"})
    answer("/cancel")
    answer("", legacy_tracker="text")
    answer("/cancel")
    rows.append({"message": {"voice": entries.VOICE, "caption": "Synthetic old caption"},
                 "legacy_tracker": "text"})
    answer("/cancel")
    answer("Synthetic recovered old pending", legacy_tracker="text")
    answer("2026-11-18 04:00Z")
    answer("Synthetic recovered legacy entry", recover=True)
    return rows


def model_cases():
    rows = []
    for name, tail in [
        ("text", "blue"),
        ("text", "not blue"),
        ("text", '"blue"'),
        ("text", "low-confidence blue"),
        ("text", "clarify-response blue"),
        ("text", "none-response blue"),
        ("text", "invalid-quote blue"),
        ("integer", "3"),
        ("number", "0.5"),
        ("boolean", "yes"),
        ("sensitive", "model-consent-denied blue"),
        ("text", "provider-unavailable blue"),
    ]:
        rows.append({"message": {"text": f"Synthetic at 2026-11-18 03:00Z {tail}"},
                     "legacy_tracker": name})
        if name == "sensitive":
            rows[-1]["grant"] = ["schema"]
    rows[0]["recover"] = True
    return rows


class SyntheticProvider:
    instance_id = "model:gemini:primary"

    def __init__(self):
        self.calls = []

    def structured(self, instruction, prompt, schema):
        self.calls.append(schema.__name__)
        if schema.__name__ == "SafetyScreen":
            return schema(urgent=False, response_language="en")
        assert schema.__name__ == "TrackerExtraction"
        prompt = json.loads(prompt)
        text = prompt["text"]
        if "provider-unavailable" in text:
            raise ProviderUnavailable("Synthetic unavailable provider")
        candidate = prompt["candidate_trackers"][0]
        field = candidate["fields"][0]
        quote = text.split()[-1].strip('"')
        value = quote
        if field["schema"].get("type") == "integer":
            value = int(quote)
        elif field["schema"].get("type") == "number":
            value = float(quote)
        elif field["schema"].get("type") == "boolean":
            value = True
        result = {
            "schema_version": "tracker.nl.v1", "intent": "create_entry",
            "definition_version_id": candidate["definition_version_id"],
            "start": "2026-11-18T03:00:00+00:00",
            "start_evidence": {"start": 0, "end": len(text), "quote": text},
            "fields": [{"field_id": field["field_id"], "value": value,
                        "evidence": {"start": text.rindex(quote), "end": text.rindex(quote) + len(quote), "quote": quote}}],
            "confidence": 0.99,
        }
        if "low-confidence" in text:
            result["confidence"] = 0.5
        if "clarify-response" in text:
            result.update(intent="clarify", clarification="Synthetic clarification")
        if "none-response" in text:
            result["intent"] = "none"
        if "invalid-quote" in text:
            result["fields"][0]["evidence"]["quote"] = "different synthetic quote"
        return schema.model_validate(result)


def nl_snapshot(session, conversation_cases):
    result = entries.snapshot(session)
    identities = entries.versions(session)
    for event in session.scalars(select(Event).where(Event.kind.startswith("user.synthetic_chat_"))):
        identities[str(event.id)] = f"entry:{event.kind}:{event.start.isoformat()}"
    states = {}
    for index, case in enumerate(conversation_cases()):
        if "legacy_tracker" not in case or not (case["message"].get("text") or case["message"].get("caption")):
            continue
        version = session.scalar(select(EventDefinitionVersion).join(EventDefinition).where(
            EventDefinition.key == f"user.synthetic_chat_{case['legacy_tracker']}"))
        operation_id = f"telegram:{71001 + index}"
        request = NaturalLanguageRequest(text=case["message"].get("text") or case["message"].get("caption") or "", operation_id=operation_id,
                                        selected_definition_version_id=version.id)
        request_hash = sha256(request.model_dump_json(exclude_none=False).encode()).hexdigest()
        identity = sha256(f"telegram:42\0{operation_id}".encode()).hexdigest()
        for prefix in ["nl-context", "nl-operation"]:
            state = session.get(AppState, f"{prefix}:{identity}", populate_existing=True)
            if state:
                assert state.value["request_hash"] == request_hash
                states[f"{prefix}:{operation_id}"] = entries.normalized(
                    {key: value for key, value in state.value.items() if key != "request_hash"},
                    identities,
                )
    result["nl_state"] = states
    result["provenance"] = [
        {"kind": event.kind, "start": event.start.isoformat(),
         "original_text": event.original_text, "evidence_refs": entries.normalized(event.evidence_refs,identities)}
        for event in session.scalars(select(Event).where(Event.kind.startswith("user.synthetic_chat_"))
                                     .order_by(Event.start,Event.kind,Event.id))
    ]
    return result


def model_snapshot(session):
    return nl_snapshot(session, model_cases)


def legacy_snapshot(session):
    return nl_snapshot(session, cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--locale", choices=["en", "ru"], default="en")
    parser.add_argument("--model", action="store_true")
    args = parser.parse_args()
    engine = isolated_engine()
    try:
        provider = SyntheticProvider() if args.model else None
        results = entries.capture(engine, args.locale, seed_data=seed,
                                  conversation_cases=model_cases if args.model else cases,
                                  provider=provider,
                                  capture_state=model_snapshot if args.model else legacy_snapshot)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        print(f"Captured {len(results)} legacy tracker steps in {args.locale}")
        if provider:
            print(f"Synthetic calls {provider.calls}")
    finally:
        engine.dispose()
