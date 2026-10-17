from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from garmin_ai import agent
from garmin_ai.archive import LocalArchive
from garmin_ai.claims import TimestampClaim, verified_timestamps
from garmin_ai.config import Settings
from garmin_ai.ingest import ingest
from garmin_ai.models import AppState

ZONE = "Europe/Bratislava"
START_PATH = ["rows", 0, "main_interval", "start"]
END_PATH = ["rows", 0, "main_interval", "end"]
START = "2026-10-15T21:42:37+00:00"
END = "2026-10-16T05:30:12+00:00"
EVIDENCE = [
    {
        "id": 1,
        "tool": "analysis_sleep",
        "result": {"rows": [{"main_interval": {"start": START, "end": END}}]},
    }
]


def claim(value=START, path=None, evidence_id=1):
    return TimestampClaim(evidence_id=evidence_id, path=path or START_PATH, value=value)


def test_bedtime_keeps_previous_date_and_converts_to_owner_timezone():
    assert verified_timestamps([claim()], EVIDENCE, {1}, ZONE) == [
        "Начало сна по Garmin: 15.10.2026 в 23:42:37 (Europe/Bratislava, +0200)."
    ]
    assert verified_timestamps([claim(END, END_PATH)], EVIDENCE, {1}, ZONE) == [
        "Пробуждение по Garmin: 16.10.2026 в 07:30:12 (Europe/Bratislava, +0200)."
    ]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-15T22:17:00+00:00", "16.10.2026 в 00:17:00"),
        ("2026-10-25T00:30:19+00:00", "25.10.2026 в 02:30:19 (Europe/Bratislava, +0200)"),
        ("2026-10-25T01:30:41+00:00", "25.10.2026 в 02:30:41 (Europe/Bratislava, +0100)"),
        ("2026-10-15T21:42:00Z", "15.10.2026 в 23:42:00"),
    ],
)
def test_midnight_and_dst_are_converted_by_program(value, expected):
    evidence = [{"id": 1, "tool": "synthetic", "result": {"at": value}}]
    assert expected in verified_timestamps([claim(value, ["at"])], evidence, {1}, ZONE)[0]


@pytest.mark.parametrize(
    "item,selected",
    [
        (claim(END), {1}),  # Real timestamp at the wrong source path.
        (claim(path=["rows", "0", "main_interval", "start"]), {1}),
        (claim(path=["rows", 2, "main_interval", "start"]), {1}),
        (claim(), {2}),
        (claim(evidence_id=2), {1, 2}),
    ],
)
def test_timestamp_requires_exact_successful_cited_field(item, selected):
    with pytest.raises(ValueError):
        verified_timestamps([item], EVIDENCE, selected, ZONE)


@pytest.mark.parametrize("value", ["2026-10-15", "2026-10-15T21:42:00", "not a timestamp"])
def test_date_only_naive_or_invalid_string_cannot_be_rendered(value):
    evidence = [{"id": 1, "tool": "synthetic", "result": {"at": value}}]
    with pytest.raises(ValueError):
        verified_timestamps([claim(value, ["at"])], evidence, {1}, ZONE)


def test_error_result_cannot_support_timestamp():
    evidence = [{**EVIDENCE[0], "result": {**EVIDENCE[0]["result"], "error": "unavailable"}}]
    with pytest.raises(ValueError):
        verified_timestamps([claim()], evidence, {1}, ZONE)


def test_out_of_range_local_timestamp_is_rejected():
    value = "9999-12-31T23:30:00+00:00"
    evidence = [{"id": 1, "tool": "synthetic", "result": {"at": value}}]
    with pytest.raises(ValueError):
        verified_timestamps([claim(value, ["at"])], evidence, {1}, ZONE)


@pytest.mark.parametrize("value", [True, 123, None, ""])
def test_timestamp_schema_rejects_coercion(value):
    with pytest.raises(ValidationError):
        claim(value)


@pytest.mark.parametrize("start", [START, "2026-10-15T22:17:00+00:00"])
def test_sleep_question_uses_one_day_and_finishes_with_verified_time(db, tmp_path, start):
    ingest(
        db,
        LocalArchive(tmp_path),
        "sleep",
        "2026-10-16",
        {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": int(datetime.fromisoformat(start).timestamp() * 1000),
                "sleepEndTimestampGMT": int(datetime.fromisoformat(END).timestamp() * 1000),
            }
        },
        ZONE,
    )

    class Provider:
        def __init__(self):
            self.calls = 0

        def structured(self, instruction, prompt, schema):
            import json

            self.calls += 1
            data = json.loads(prompt)
            if self.calls == 1:
                assert "timestamp_claims" in instruction
                assert data["planning_only"] is True
                assert data["now"] == "2026-10-16T09:00:00+02:00"
                return agent.AgentStep(
                    calls=[
                        agent.ReadCall(
                            name="analysis_sleep",
                            arguments_json='{"start":"2026-10-16","end":"2026-10-16"}',
                        )
                    ]
                )
            assert self.calls == 2
            result = data["evidence"][0]["result"]
            assert result["rows"][0]["main_interval"]["start"] == start
            return agent.AgentStep(evidence_ids=[1], timestamp_claims=[claim(start)])

    provider = Provider()
    budget = agent.AnalysisBudget()
    response = agent.answer_question(
        db,
        provider,
        "Во сколько началась указанная ночь сна?",
        Settings(timezone=ZONE),
        datetime(2026, 10, 16, 7, tzinfo=UTC),
        budget=budget,
        update_id=1,
    )
    assert "Начало сна по Garmin:" in response
    assert ("15.10.2026 в 23:42:37" if start == START else "16.10.2026 в 00:17:00") in response
    assert budget.model_calls == 2 and budget.input_bytes < agent.ANALYSIS_PROMPT_BYTES
    assert response != agent.ANALYSIS_BUDGET_NOTICE
    assert db.get(AppState, "analysis:conversation:pending").value["turn"]["answer"] == response


def test_fabricated_sleep_time_is_not_sent_or_remembered(db, monkeypatch):
    monkeypatch.setattr(agent, "call_tool", lambda *args: EVIDENCE[0]["result"])

    class Provider:
        steps = iter(
            [
                agent.AgentStep(calls=[agent.ReadCall(name="analysis_sleep", arguments_json="{}")]),
                agent.AgentStep(evidence_ids=[1], timestamp_claims=[claim(END)]),
            ]
        )

        def structured(self, *args):
            return next(self.steps)

    result = agent.answer_question(
        db,
        Provider(),
        "Во сколько началась указанная ночь сна?",
        Settings(),
        datetime.now(UTC),
        update_id=1,
    )
    assert "Не удалось подтвердить" in result
    assert db.get(AppState, "analysis:conversation:pending") is None
