"""Model planning exposes only currently permitted tool contracts."""

import json
from datetime import UTC, datetime

from garmin_ai.agent import AgentStep, ReadCall, answer_question
from garmin_ai.config import Settings
from garmin_ai.models import AppState
from garmin_ai.scenario_packs import ensure_scenario_packs
from garmin_ai.tools import model_tool_descriptions

NOW = datetime(2026, 10, 22, 14, tzinfo=UTC)


def names(session):
    return {row["name"] for row in model_tool_descriptions(session)}


def test_planner_only_sees_tools_allowed_by_selected_categories(db):
    db.add(AppState(key="preferences:onboarding", value={"model_categories": ["diary"]}))
    db.flush()
    visible = names(db)
    assert {"events", "generic_analysis", "resource_catalog"} <= visible
    assert "metric_series" not in visible
    assert "analysis_sleep" not in visible
    assert "analysis_stress_by_hour" not in visible

    class Provider:
        def structured(self, _instruction, prompt, _schema):
            assert {row["name"] for row in json.loads(prompt)["tools"]} == visible
            return AgentStep(urgent_safety=True)

    answer_question(db, Provider(), "synthetic request", Settings(_env_file=None), NOW)


def test_no_selected_model_categories_exposes_no_read_tools(db):
    db.add(AppState(key="preferences:onboarding", value={"model_categories": []}))
    db.flush()
    assert names(db) == set()


def test_planner_respects_disabled_scenario_packs(db):
    packs = ensure_scenario_packs(db)
    packs["wellbeing"].llm_enabled = False
    packs["sleep"].llm_enabled = True
    db.flush()
    visible = names(db)
    assert "analysis_stress_by_hour" not in visible
    assert "wellbeing_observations" not in visible
    assert "analysis_sleep" in visible
    assert "generic_analysis" in visible


def test_later_round_rechecks_consent_and_does_not_run_revoked_tool(db):
    saved = AppState(key="preferences:onboarding", value={"model_categories": ["health", "diary"]})
    db.add(saved)
    db.flush()

    class Provider:
        def __init__(self):
            self.calls = 0

        def structured(self, _instruction, prompt, _schema):
            payload = json.loads(prompt)
            self.calls += 1
            if self.calls == 1:
                assert "data_freshness" in {item["name"] for item in payload["tools"]}
                saved.value = {"model_categories": ["diary"]}
                db.flush()
                return AgentStep(calls=[ReadCall(name="data_freshness", arguments_json="{}")])
            assert "data_freshness" not in {item["name"] for item in payload["tools"]}
            assert "error" in payload["evidence"][0]["result"]
            return AgentStep(urgent_safety=True)

    provider = Provider()
    answer_question(db, provider, "synthetic request", Settings(_env_file=None), NOW)
    assert provider.calls == 2
