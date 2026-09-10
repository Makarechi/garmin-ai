"""Owner-defined drink recipes; button callbacks freeze the selected payload."""

import hashlib
import json
from uuid import UUID

from pydantic import Field, model_validator

from garmin_ai.events import Caffeine, StrictModel, caffeine_total


class CaffeinePreset(StrictModel):
    id: UUID
    name: str = Field(min_length=1, max_length=100)
    recipe: Caffeine

    @model_validator(mode="after")
    def named(self):
        if not self.name.strip():
            raise ValueError("Preset name cannot be blank")
        return self


def callback(preset):
    digest = hashlib.sha256(
        json.dumps(preset.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"c:{preset.id.hex}:{digest}"


def label(preset):
    total = caffeine_total(preset.recipe.model_dump())
    amount = "кофеин неизвестен"
    if total["min"] is not None and total["max"] is not None:
        amount = f"{total['min']:g}–{total['max']:g} мг"
    elif total["estimate"] is not None:
        amount = f"≈{total['estimate']:g} мг"
    elif total["min"] is not None:
        amount = f"от {total['min']:g} мг"
    elif total["max"] is not None:
        amount = f"до {total['max']:g} мг"
    return f"☕ {preset.name[:50]} · {amount}"


def keyboard(presets):
    return {
        "inline_keyboard": [
            [{"text": label(preset), "callback_data": callback(preset)}] for preset in presets
        ]
        + [[{"text": "Кофе без уточнения", "callback_data": "coffee:unspecified"}]]
    }
