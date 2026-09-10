from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from garmin_ai import agent
from garmin_ai.claims import NumericClaim, verified_numbers
from garmin_ai.config import Settings
from garmin_ai.models import AppState

EVIDENCE = [
    {
        "id": 1,
        "tool": "personal_baseline",
        "result": {"mean": 78, "max": 87, "rows": [{"value": 0}]},
    }
]


def claim(value=78, path=None, evidence_id=1):
    return NumericClaim(evidence_id=evidence_id, path=path or ["mean"], value=value)


def test_exact_field_required_even_if_fabricated_number_exists_elsewhere():
    with pytest.raises(ValueError):
        verified_numbers([claim(87)], EVIDENCE, {1})
    assert verified_numbers([claim()], EVIDENCE, {1}) == ["personal_baseline /mean: 78"]
    assert verified_numbers([claim(0, ["rows", 0, "value"])], EVIDENCE, {1})


@pytest.mark.parametrize(
    "path", [["missing"], ["rows", 2, "value"], ["rows", "0", "value"], ["mean", "extra"]]
)
def test_missing_and_wrong_container_paths_rejected(path):
    with pytest.raises(ValueError):
        verified_numbers([claim(path=path)], EVIDENCE, {1})


@pytest.mark.parametrize("value", [None, True, "78", float("nan"), float("inf")])
def test_non_numeric_evidence_never_becomes_a_number(value):
    with pytest.raises(ValueError):
        verified_numbers(
            [claim(1 if value is True else 78)],
            [{"id": 1, "tool": "synthetic", "result": {"mean": value}}],
            {1},
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"value": True},
        {"value": "78"},
        {"value": float("nan")},
        {"path": [True]},
        {"path": [-1]},
        {"evidence_id": True},
    ],
)
def test_schema_rejects_coercion_and_nonfinite_claims(fields):
    with pytest.raises(ValidationError):
        NumericClaim(**({"evidence_id": 1, "path": ["mean"], "value": 78} | fields))


def test_error_or_uncited_evidence_cannot_support_number():
    with pytest.raises(ValueError):
        verified_numbers([claim()], EVIDENCE, {2})
    with pytest.raises(ValueError):
        verified_numbers(
            [claim()],
            [{"id": 1, "tool": "synthetic", "result": {"error": "failed", "mean": 78}}],
            {1},
        )


class Provider:
    def __init__(self, final):
        self.steps = iter(
            [
                agent.AgentStep(
                    calls=[agent.ReadCall(name="personal_baseline", arguments_json="{}")]
                ),
                final,
            ]
        )

    def structured(self, *args):
        return next(self.steps)


@pytest.mark.parametrize("text", ["Среднее 87", "Среднее ８７", "Среднее ⁸⁷"])
def test_unverified_narrative_is_not_sent_or_remembered(db, monkeypatch, text):
    monkeypatch.setattr(agent, "call_tool", lambda *args: EVIDENCE[0]["result"])
    final = agent.AgentStep(answer=text, evidence_ids=[1])
    result = agent.answer_question(
        db, Provider(final), "synthetic", Settings(), datetime.now(UTC), update_id=1
    )
    assert "Не удалось подтвердить числа" in result
    assert db.get(AppState, "analysis:conversation") is None


def test_verified_answer_is_rendered_from_evidence_and_remembered(db, monkeypatch):
    monkeypatch.setattr(agent, "call_tool", lambda *args: EVIDENCE[0]["result"])
    final = agent.AgentStep(
        answer="Среднее ниже максимума.", evidence_ids=[1], numeric_claims=[claim()]
    )
    result = agent.answer_question(
        db, Provider(final), "synthetic", Settings(), datetime.now(UTC), update_id=1
    )
    assert "personal_baseline /mean: 78" in result
    assert db.get(AppState, "analysis:conversation").value["turns"][0]["answer"] == result


def test_claims_only_answer_supported(db, monkeypatch):
    monkeypatch.setattr(agent, "call_tool", lambda *args: EVIDENCE[0]["result"])
    final = agent.AgentStep(evidence_ids=[1], numeric_claims=[claim()])
    assert "mean: 78" in agent.answer_question(
        db, Provider(final), "synthetic", Settings(), datetime.now(UTC)
    )
