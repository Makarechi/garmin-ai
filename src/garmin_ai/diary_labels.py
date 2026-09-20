"""Diary display labels without importing an optional channel SDK."""

from datetime import UTC, datetime


def diary_label(event):
    payload = event.payload
    if event.kind == "activity_effort":
        return f"Тяжесть тренировки: {payload['perceived_exertion']}/10, активность {payload['activity_id']}"
    if event.kind == "wellbeing_observation":
        from garmin_ai.wellbeing import label

        return label(payload)
    if event.kind == "caffeine_log_complete":
        return "Полнота дневника кофеина: " + payload["description"]
    if event.kind == "headache_observation":
        from garmin_ai.events import headache_observation_label

        return headache_observation_label(payload)
    if event.kind == "caffeine":
        from garmin_ai.events import caffeine_total

        total = caffeine_total(payload)
        label = f"Кофе: {payload['beverage']}, порций: {payload.get('servings', 1)}"
        if total["min"] is not None and total["max"] is not None:
            label += f"; всего кофеина {total['min']:g}–{total['max']:g} мг"
        elif total["estimate"] is not None:
            qualifier = "около " if total["provenance"] != "reported_label" else ""
            label += f"; всего кофеина {qualifier}{total['estimate']:g} мг"
        elif total["min"] is not None:
            label += f"; всего кофеина не менее {total['min']:g} мг"
        elif total["max"] is not None:
            label += f"; всего кофеина не более {total['max']:g} мг"
        else:
            return label + "; суммарная доза неизвестна"
        return label + (
            " (по этикетке)"
            if total["provenance"] == "reported_label"
            else " (оценка)"
            if total["provenance"] == "estimated"
            else " (источник дозы не указан)"
        )
    if event.kind == "symptom_observation":
        severity = payload.get("severity")
        parts = [
            "Наблюдение симптомов: боль "
            + (f"{severity}/10" if severity is not None else "не указана")
        ]
        if payload.get("aura") is not None:
            parts.append("аура: " + ("да" if payload["aura"] else "нет"))
        if payload.get("symptoms"):
            parts.append(", ".join(payload["symptoms"]))
        if payload.get("impact"):
            parts.append(payload["impact"])
        return "; ".join(parts)
    if event.kind == "migraine":
        severity = payload.get("severity")
        return (
            "Мигрень"
            + (f", {severity}/10" if severity is not None else "")
            + (
                ", завершена"
                if event.end and event.end <= datetime.now(UTC)
                else ", ещё не завершена"
            )
        )
    if event.kind == "medication":
        from garmin_ai.events import medication_label

        return medication_label(payload)
    return payload.get("description", "Запись дневника")
