"""Bounded, data-only scenario packs for owner-confirmed tracker setup."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import delete, select

from garmin_ai.events import Conflict, StrictModel, lock_writes
from garmin_ai.metric_definitions import METHODS
from garmin_ai.models import AppState, EventDefinition
from garmin_ai.scenario_packs import PACKS, pack_enabled
from garmin_ai.tracker_forms import (
    TrackerConfirmation,
    TrackerSetupDraft,
    confirm_tracker,
    definition_spec,
    preview_tracker,
)

FORMAT = "garmin-ai-community-pack-v1"
PREVIEW_PREFIX = "community-pack-preview:"
IMPORT_PREFIX = "community-pack-import:"


class AnalysisRecipe(StrictModel):
    label: str = Field(min_length=1, max_length=120)
    operation: Literal["query_observations", "aggregate_metric", "compare_periods"]
    metric_key: str = Field(pattern=r"^(?:user|system)\.[a-z][a-z0-9_.-]{0,126}$")
    method: (
        Literal[
            "latest",
            "median",
            "distribution",
            "sum",
            "mean",
            "counts",
            "min",
            "max",
            "delta",
            "mode",
            "count_true",
            "rate",
        ]
        | None
    ) = None
    limitation: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def coherent_operation(self):
        if self.operation == "query_observations":
            if self.method is not None:
                raise ValueError("Observation queries return raw rows and cannot specify a method")
        elif self.method is None:
            raise ValueError("Aggregate and comparison recipes require a method")
        return self


class CommunityPack(StrictModel):
    format: Literal["garmin-ai-community-pack-v1"] = FORMAT
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    version: int = Field(ge=1, le=1000, strict=True)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    required_packs: list[str] = Field(default_factory=list, max_length=6)
    trackers: list[TrackerSetupDraft] = Field(min_length=1, max_length=8)
    analysis: list[AnalysisRecipe] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def safe_pack(self):
        if len({draft.key for draft in self.trackers}) != len(self.trackers):
            raise ValueError("Pack tracker keys must be distinct")
        if len(set(self.required_packs)) != len(self.required_packs) or set(
            self.required_packs
        ) - set(PACKS):
            raise ValueError("Pack refers to unknown or duplicate system packs")
        if any(draft.reminder_enabled or draft.reminder_time for draft in self.trackers):
            raise ValueError("Imported packs cannot enable or schedule reminders")
        if any(draft.privacy != "sensitive" for draft in self.trackers):
            raise ValueError("Imported trackers require separate destination consent")
        required_by_metric = {
            "system.sleep_score": "sleep",
            "system.training_readiness_score": "training",
        }
        for recipe in self.analysis:
            required = required_by_metric.get(recipe.metric_key)
            if required and required not in self.required_packs:
                raise ValueError(f"{recipe.metric_key} requires the {required} system pack")
        for draft in self.trackers:
            definition_spec(draft)
        allowed_metrics = {
            "system.sleep_score": METHODS["ordinal"],
            "system.training_readiness_score": METHODS["ordinal"],
        }
        for draft in self.trackers:
            for field in draft.fields:
                value_kind = {
                    "scale": "ordinal",
                    "choice": "nominal",
                    "boolean": "boolean",
                    "integer": "physical_number",
                    "number": "physical_number",
                }.get(field.kind)
                if value_kind is None:
                    continue
                if field.metric_semantics in {"event_total", "event_count"}:
                    value_kind = "increment"
                elif field.metric_semantics == "interval_total":
                    value_kind = "interval_total"
                elif field.metric_semantics == "cumulative_counter":
                    value_kind = "cumulative_counter"
                allowed_metrics[f"user.{draft.key}.{field.key}"] = METHODS[value_kind]
            if draft.derived_duration:
                allowed_metrics[f"user.{draft.key}.elapsed_minutes"] = METHODS["interval_total"]
        for recipe in self.analysis:
            methods = allowed_metrics.get(recipe.metric_key)
            if methods is None or (recipe.method is not None and recipe.method not in methods):
                raise ValueError("Analysis recipe does not match a permitted metric operation")
        if (
            len(
                json.dumps(
                    self.model_dump(mode="json", exclude_unset=True), ensure_ascii=False
                ).encode("utf-8")
            )
            > 64_000
        ):
            raise ValueError("Pack exceeds the size limit")
        return self


class PackConfirmation(StrictModel):
    pack: CommunityPack
    confirmation_token: str = Field(pattern=r"^[0-9a-f]{64}$")


def _digest(pack: CommunityPack) -> str:
    data = json.dumps(pack.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def _state_key(pack: CommunityPack) -> str:
    return f"{IMPORT_PREFIX}{pack.key}:{pack.version}"


def _changes(session, pack: CommunityPack, digest: str):
    keys = [f"user.{draft.key}" for draft in pack.trackers]
    existing = {
        row.key: row
        for row in session.scalars(select(EventDefinition).where(EventDefinition.key.in_(keys)))
    }
    saved = session.get(AppState, _state_key(pack))
    if saved and saved.value.get("hash") != digest:
        return [{"definition_key": key, "status": "version_conflict"} for key in keys]
    installed = (
        saved.value.get("definitions", {}) if saved and saved.value.get("hash") == digest else {}
    )
    changes = []
    for key in keys:
        row = existing.get(key)
        if row is None:
            status = "create"
        elif row.status != "active":
            status = "conflict"
        elif installed.get(key) == {"id": str(row.id), "version": row.current_version}:
            status = "already_installed"
        else:
            status = "conflict"
        changes.append({"definition_key": key, "status": status})
    return changes


def preview_community_pack(session, payload, *, reveal_dependencies=True):
    pack = CommunityPack.model_validate(payload)
    digest = _digest(pack)
    changes = _changes(session, pack, digest)
    token = secrets.token_hex(32)
    session.execute(
        delete(AppState).where(
            AppState.key.startswith(PREVIEW_PREFIX),
            AppState.value["expires_at"].as_string() < datetime.now(UTC).isoformat(),
        )
    )
    session.add(
        AppState(
            key=PREVIEW_PREFIX + token,
            value={
                "hash": digest,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            },
        )
    )
    session.flush()
    return {
        "confirmation_token": token,
        "pack_key": pack.key,
        "version": pack.version,
        "changes": changes,
        "required_packs": [
            {
                "key": key,
                "tracking_enabled": pack_enabled(session, key) if reveal_dependencies else None,
                "collection_enabled": (
                    pack_enabled(session, key, "collection") if reveal_dependencies else None
                ),
            }
            for key in pack.required_packs
        ],
        "permissions": {
            "create_trackers": any(row["status"] == "create" for row in changes),
            "enable_reminders": False,
            "enable_external_sources": False,
            "grant_model_or_channel_access": False,
        },
        "analysis": [item.model_dump(mode="json") for item in pack.analysis],
        "limitations": pack.limitations,
    }


def import_community_pack(session, confirmation: PackConfirmation, *, actor: str):
    confirmation = PackConfirmation.model_validate(confirmation)
    pack = confirmation.pack
    digest = _digest(pack)
    lock_writes(session)
    preview = session.get(AppState, PREVIEW_PREFIX + confirmation.confirmation_token)
    if (
        preview is None
        or preview.value.get("hash") != digest
        or datetime.fromisoformat(preview.value["expires_at"]) <= datetime.now(UTC)
    ):
        raise Conflict("Pack preview changed; preview it again")
    changes = _changes(session, pack, digest)
    if any(row["status"] in {"conflict", "version_conflict"} for row in changes):
        raise Conflict(
            "Pack version or tracker key conflicts with an existing installation; rename and preview again"
        )
    session.delete(preview)
    created = []
    for draft in pack.trackers:
        key = f"user.{draft.key}"
        if next(row for row in changes if row["definition_key"] == key)["status"] != "create":
            continue
        tracker_preview = preview_tracker(session, draft)
        created.append(
            confirm_tracker(
                session,
                TrackerConfirmation(
                    draft=draft, confirmation_token=tracker_preview["confirmation_token"]
                ),
                actor=actor,
            )
        )
    keys = [row["definition_key"] for row in changes]
    definitions = {
        row.key: {"id": str(row.id), "version": row.current_version}
        for row in session.scalars(select(EventDefinition).where(EventDefinition.key.in_(keys)))
    }
    # A pack records provenance only. Historical definition versions remain untouched.
    state = session.get(AppState, _state_key(pack))
    value = {
        "hash": digest,
        "definitions": definitions,
        "installed_at": datetime.now(UTC).isoformat(),
    }
    if state is None:
        session.add(AppState(key=_state_key(pack), value=value))
    elif created:
        state.value = value
    session.flush()
    return {"pack_key": pack.key, "version": pack.version, "created": created, "changes": changes}


def catalog():
    """Return validated synthetic-first recipes, with no integration side effects."""

    return [CommunityPack.model_validate(item).model_dump(mode="json") for item in _CATALOG]


_CATALOG = [
    {
        "key": "sleep_energy",
        "version": 1,
        "title": "Сон и субъективная энергия",
        "description": "Отмечайте энергию отдельно от оценки сна Garmin.",
        "required_packs": ["sleep"],
        "trackers": [
            {
                "key": "daily_energy",
                "name": "Энергия за день",
                "locale": "ru",
                "privacy": "sensitive",
                "fields": [
                    {
                        "key": "energy",
                        "label": "Энергия",
                        "kind": "scale",
                        "minimum": 1,
                        "maximum": 5,
                    },
                    {"key": "context", "label": "Контекст", "kind": "text", "required": False},
                ],
            }
        ],
        "analysis": [
            {
                "label": "Оценки сна",
                "operation": "aggregate_metric",
                "metric_key": "system.sleep_score",
                "method": "median",
                "limitation": "Только дни с доступной оценкой Garmin; не диагноз.",
            },
            {
                "label": "Субъективная энергия",
                "operation": "aggregate_metric",
                "metric_key": "user.daily_energy.energy",
                "method": "median",
                "limitation": "Пропущенный день не означает низкую энергию.",
            },
        ],
        "limitations": [
            "Оценка Garmin и самооценка имеют разные шкалы и не объединяются в один показатель."
        ],
    },
    {
        "key": "running_effort",
        "version": 1,
        "title": "Бег и субъективная тяжесть",
        "description": "Записывайте воспринятое усилие рядом с сохранённой активностью.",
        "required_packs": ["training"],
        "trackers": [
            {
                "key": "run_effort",
                "name": "Ощущения после бега",
                "locale": "ru",
                "privacy": "sensitive",
                "fields": [
                    {
                        "key": "effort",
                        "label": "Тяжесть",
                        "kind": "scale",
                        "minimum": 1,
                        "maximum": 10,
                    },
                    {
                        "key": "activity_note",
                        "label": "Какая пробежка",
                        "kind": "text",
                        "required": False,
                    },
                ],
            }
        ],
        "analysis": [
            {
                "label": "Тяжесть пробежек",
                "operation": "aggregate_metric",
                "metric_key": "user.run_effort.effort",
                "method": "median",
                "limitation": "Совпадение даты с активностью не доказывает, что записи относятся к одной пробежке.",
            }
        ],
        "limitations": [
            "Активность Garmin и самооценка остаются отдельными записями; для сопоставления проверяйте время вручную."
        ],
    },
    {
        "key": "focus_walks",
        "version": 1,
        "title": "Концентрация и прогулки",
        "description": "Полностью ручной дневник, пригодный без часов и Garmin.",
        "trackers": [
            {
                "key": "focus_walk",
                "name": "Концентрация и прогулка",
                "locale": "ru",
                "privacy": "sensitive",
                "fields": [
                    {
                        "key": "focus",
                        "label": "Концентрация",
                        "kind": "scale",
                        "minimum": 1,
                        "maximum": 5,
                    },
                    {
                        "key": "walk_minutes",
                        "label": "Прогулка, минуты",
                        "kind": "integer",
                        "unit": "minutes",
                        "minimum": 0,
                        "maximum": 1440,
                        "metric_semantics": "event_total",
                        "required": False,
                    },
                    {"key": "context", "label": "Контекст", "kind": "text", "required": False},
                ],
            }
        ],
        "analysis": [
            {
                "label": "Концентрация",
                "operation": "aggregate_metric",
                "metric_key": "user.focus_walk.focus",
                "method": "median",
                "limitation": "Самооценка не является клинической шкалой.",
            },
            {
                "label": "Длительность прогулок",
                "operation": "aggregate_metric",
                "metric_key": "user.focus_walk.walk_minutes",
                "method": "sum",
                "limitation": "Суммируются только явно заполненные записи; пропуски неизвестны.",
            },
        ],
        "limitations": ["Связь прогулок и концентрации нельзя считать причинной по этим данным."],
    },
]
