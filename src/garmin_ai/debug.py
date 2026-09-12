"""Opt-in operational notices containing only fixed, public labels."""

from datetime import UTC, datetime

from garmin_ai.jobs import enqueue
from garmin_ai.models import AppState

KEY = "telegram:debug"
KINDS = {
    "telegram_poll": "получение сообщений Telegram",
    "telegram_update": "обработка сообщения",
    "telegram_control": "команда бота",
    "garmin_endpoint": "показатели Garmin",
    "garmin_activities": "тренировки Garmin",
    "garmin_fit": "файл тренировки",
    "agent_insights": "анализ данных",
    "agent_proactive": "вопросы бота",
    "backup": "резервная копия",
    "storage_check": "проверка хранилища",
}
ERRORS = {
    "ProviderUnavailable": "Gemini недоступен",
    "ProviderRateLimited": "лимит Gemini",
    "ProviderOutputInvalid": "Gemini вернул неподходящий ответ",
    "ProviderConsentRequired": "нужно согласие на Gemini",
    "AuthenticationRequired": "нужен вход в Garmin",
    "AccountMismatch": "не совпадает владелец Garmin",
    "AccountEnrollmentRequired": "нужно подтвердить владельца Garmin",
    "RetryAfter": "Telegram просит подождать",
    "TimedOut": "истекло время ожидания",
    "NetworkError": "ошибка соединения с Telegram",
    "GarminConnectTooManyRequestsError": "лимит запросов Garmin",
    "GarminConnectConnectionError": "ошибка соединения с Garmin",
    "CircuitOpen": "соединение с Garmin временно приостановлено после ошибок",
    "DeliveryUncertain": "доставка ответа не подтверждена",
}


def enabled(session):
    row = session.get(AppState, KEY, populate_existing=True)
    return bool(row and row.value.get("enabled"))


def queue_error_notice(session, kind, error, now=None):
    if error == "DiaryDeferred" or kind not in KINDS or not enabled(session):
        return
    now = now or datetime.now(UTC)
    # Unknown exception names and exception messages never enter the chat.
    category = error if error in ERRORS else "internal"
    row = session.get(AppState, KEY, populate_existing=True)
    generation = [row.value.get("message_at"), row.value.get("update_id")]
    enqueue(
        session,
        "telegram_debug_notice",
        {"kind": kind, "error": category, "generation": generation},
        f"debug:{kind}:{category}:{generation[0]}:{generation[1]}:{int(now.timestamp()) // 600}",
        now,
    )


def notice_text(payload):
    kind = KINDS.get(payload.get("kind"), "задача приложения")
    error = ERRORS.get(payload.get("error"), "внутренняя ошибка")
    return f"Диагностика: {kind} — {error}.\nОтключить уведомления: /debug off"


def can_deliver(session, payload):
    row = session.get(AppState, KEY, populate_existing=True)
    return bool(
        row
        and row.value.get("enabled")
        and payload.get("generation") == [row.value.get("message_at"), row.value.get("update_id")]
    )
