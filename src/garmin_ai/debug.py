"""Opt-in operational notices containing only fixed, public labels."""

from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from garmin_ai.models import AppState, Job

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
    "ProviderAuthError": "ошибка авторизации Gemini",
    "ProviderModelUnavailable": "модель Gemini недоступна: проверьте настройки",
    "ProviderCooldown": "запросы к Gemini временно приостановлены",
    "ProviderRequestInvalid": "Gemini отклонил запрос",
    "AuthenticationRequired": "нужен вход в Garmin",
    "AccountMismatch": "не совпадает владелец Garmin",
    "AccountEnrollmentRequired": "нужно подтвердить владельца Garmin",
    "AccountError": "не удалось подтвердить учётную запись Garmin",
    "RetryAfter": "Telegram просит подождать",
    "TimedOut": "истекло время ожидания",
    "NetworkError": "ошибка соединения с Telegram",
    "GarminConnectTooManyRequestsError": "лимит запросов Garmin",
    "GarminConnectConnectionError": "ошибка соединения с Garmin",
    "CircuitOpen": "соединение с Garmin временно приостановлено после ошибок",
    "DeliveryUncertain": "доставка ответа не подтверждена",
    "BackupSpaceInsufficient": "недостаточно места для резервной копии",
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
    payload = {
        "kind": kind,
        "error": category,
        "generation": generation,
        "expires_at": now.timestamp() + 600,
    }
    session.execute(
        insert(Job)
        .values(
            kind="telegram_debug_notice",
            payload=payload,
            dedup_key=f"debug:{kind}:{category}:{generation[0]}:{generation[1]}:{int(now.timestamp()) // 600}",
            run_at=now,
        )
        .on_conflict_do_update(
            index_elements=[Job.dedup_key],
            set_={
                "payload": Job.payload.op("||")(
                    func.jsonb_build_object(
                        "expires_at",
                        func.greatest(Job.payload["expires_at"].as_float(), payload["expires_at"]),
                    )
                )
            },
            where=Job.status == "pending",
        )
    )


def notice_text(payload):
    kind = KINDS.get(payload.get("kind"), "задача приложения")
    error = ERRORS.get(payload.get("error"), "внутренняя ошибка")
    return f"Диагностика: {kind} — {error}.\nОтключить уведомления: /debug off"


def can_deliver(session, payload, now=None):
    now = now or datetime.now(UTC)
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)) or expires_at <= now.timestamp():
        return False
    row = session.get(AppState, KEY, populate_existing=True)
    return bool(
        row
        and row.value.get("enabled")
        and payload.get("generation") == [row.value.get("message_at"), row.value.get("update_id")]
    )
