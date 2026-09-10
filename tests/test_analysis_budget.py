import json
from datetime import UTC, datetime

import pytest

from garmin_ai import agent
from garmin_ai.config import Settings


class Provider:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.prompts = []

    def structured(self, instruction, prompt, schema):
        self.prompts.append(prompt)
        return next(self.steps)


def calls(count=4):
    return agent.AgentStep(
        calls=[agent.ReadCall(name="synthetic", arguments_json="{}") for _ in range(count)]
    )


def answer(db, provider, text="synthetic"):
    return agent.answer_question(db, provider, text, Settings(), datetime.now(UTC))


def test_total_tool_limit_still_allows_bounded_final_answer(db, monkeypatch):
    executed = []
    monkeypatch.setattr(agent, "call_tool", lambda *args: executed.append(args) or {"value": 78})
    provider = Provider(
        [calls(), calls(), calls(), agent.AgentStep(answer="Result", evidence_ids=[12])]
    )
    assert "Result" in answer(db, provider)
    assert len(executed) == 12
    last = json.loads(provider.prompts[-1])
    assert last["answer_only"] and not last["tools"]
    assert last["remaining_tool_calls"] == 0


def test_more_calls_after_total_limit_are_not_executed(db, monkeypatch):
    executed = []
    monkeypatch.setattr(agent, "call_tool", lambda *args: executed.append(args) or {"value": 78})
    assert answer(db, Provider([calls()] * 4)) == agent.ANALYSIS_BUDGET_NOTICE
    assert len(executed) == 12


def test_multiple_valid_results_cannot_bypass_shared_utf8_budget(db, monkeypatch):
    executed = []
    # Each result is below compact's character limit; together their UTF-8 bytes exceed the cap.
    monkeypatch.setattr(
        agent, "call_tool", lambda *args: executed.append(args) or {"text": "я" * 12000}
    )
    provider = Provider([calls()])
    assert answer(db, provider) == agent.ANALYSIS_BUDGET_NOTICE
    assert len(executed) == 2
    assert len(provider.prompts) == 1


def test_prompt_limit_counts_utf8_before_external_call(db):
    provider = Provider([])
    assert answer(db, provider, "я" * 48000) == agent.ANALYSIS_BUDGET_NOTICE
    assert not provider.prompts


def test_cumulative_prompt_budget_includes_repeated_context(db, monkeypatch):
    monkeypatch.setattr(agent, "call_tool", lambda *args: {"value": 78})

    class CappedProvider(Provider):
        def structured(self, instruction, prompt, schema):
            used = (
                len(instruction.encode("utf-8"))
                + len(prompt.encode("utf-8"))
                + len(json.dumps(schema.model_json_schema()).encode("utf-8"))
            )
            monkeypatch.setattr(agent, "ANALYSIS_TOTAL_INPUT_BYTES", used + 1)
            return super().structured(instruction, prompt, schema)

    provider = CappedProvider([calls(1)])
    assert answer(db, provider) == agent.ANALYSIS_BUDGET_NOTICE
    assert len(provider.prompts) == 1


def test_deadline_stops_more_work_after_slow_provider(db, monkeypatch):
    clock = [0]
    monkeypatch.setattr(agent, "monotonic", lambda: clock[0])
    executed = []
    monkeypatch.setattr(agent, "call_tool", lambda *args: executed.append(args) or {})

    class SlowProvider:
        def structured(self, *args):
            clock[0] += 121
            return calls()

    assert answer(db, SlowProvider()) == agent.ANALYSIS_BUDGET_NOTICE
    assert not executed


@pytest.mark.parametrize("oversized", [True, False])
def test_oversized_result_cannot_support_answer_but_refinement_can(db, monkeypatch, oversized):
    monkeypatch.setattr(
        agent, "call_tool", lambda *args: {"text": "x" * (24001 if oversized else 5)}
    )
    provider = Provider([calls(1), agent.AgentStep(answer="Invented absence", evidence_ids=[1])])
    response = answer(db, provider)
    assert ("Invented absence" in response) is not oversized
    if oversized:
        value = json.loads(provider.prompts[-1])["evidence"][0]["result"]
        assert value["error"] == "result_too_large"
        assert "empty data" in value["instruction"]
