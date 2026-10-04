# Контракт расширений: что нужно довести до публичной гарантии

Это проектное решение, не описание уже опубликованного SDK. Развивает `integrations.py`, `channels`, `dialogue`, определения, query tools и существующую модель согласия; не заменяет их параллельными реализациями.

## 1. Общий descriptor

Для установленного расширения нужны стабильный provider ID, тип (`source`, `channel`, `model`, отдельный data-only pack), версия расширения, диапазон совместимости с публичным контрактом, schema конфигурации, capabilities и их ограничения, ссылка на документацию, maintainer и набор тестовых примеров.

Экземпляр дополнительно содержит собственный ID, настройку enabled, validated non-secret config, references на необходимые секреты и разрешённого владельца. Наличие установленного пакета не включает его автоматически.

Секреты никогда не входят в журналы ошибок, capabilities, экспорт сценария, модельный контекст или UI diagnostics. Проверка конфигурации не равна сетевой проверке подключения. Различать: не настроено, SDK отсутствует, credentials требуются, disabled, доступно локально, ожидает consent, live-проверено.

## 2. SourceAdapter

Минимальные обязанности: разрешить identity источника; вернуть capabilities с временной семантикой; читать ограниченную страницу/окно с cursor; вернуть исходный payload и reference; сообщить ошибки и минимальное время следующей допустимой попытки.

Общий ingestion, а не адаптер по своему усмотрению, выполняет сохранение raw, дедупликацию, нормализацию, lineage, invalidation и управление progress. Конкретный нормализатор поставляется рядом с source adapter и проверяется общими контрактами.

Контракт batch должен различать partial fragment, complete interval snapshot и явное удаление. Пустая страница не означает удаление истории. Сохраняются observed/effective/source-calendar/fetched/ingested времена, timezone, единицы, источник и степень подтверждения.

Учитывать flow Garmin Connect: сервер получает уже синхронизированные с Garmin данные, а не постоянно управляет часами через этот API (E07). Публичный продукт не должен обещать real-time обновления, когда они не подтверждены.

## 3. ChannelAdapter

На входе: проверить подлинность provider callback и привязку пользователя до создания команды. External user/chat/message IDs — непрозрачные строки в namespace channel instance. Message revision и event identity — разные поля. Нельзя искать владельца только по имени или номеру телефона из недоверенного payload.

На выходе: `OutboundIntent` с typed text/actions/attachment references/evidence. Адаптер рендерит возможности, но не переписывает факты. Не поддерживает голос — возвращает понятный unsupported result. Нельзя молча игнорировать вложение и отмечать весь intent доставленным.

Delivery policy включает не только `supports_buttons`, но и ограничения инициатив: наличие opt-in, возможность отправки сейчас, необходимость шаблона, expiry, лимиты размера и требования внешней платформы. WhatsApp/Viber — будущие реализации с собственным policy review, а не Telegram с другим hostname (E03–E04).

Состояния минимум: queued, sending/leased, provider_accepted, delivered, read, known_failed, uncertain, cancelled, expired. `HTTP 200` не обязан означать delivered/read. Таймаут после начала отправки не делает разрешённой отправку через второй канал.

Перед первым сетевым side effect повторно проверяются effective owner/channel, consent, pause, актуальность rule/definition, expiry, бюджет, revision и действующий lease. После revoke новая отправка недопустима; уже переданное внешнему API сообщение нельзя гарантированно отозвать.

## 4. ModelProvider и agent policy

Provider отвечает за преобразование model request в конкретный API и возврат validated candidate response/usage/error. Agent отвечает за контекст, bounded tool selection и план; application service — за разрешения, время, schema, транзакцию и audit. Результат LLM сам по себе не даёт разрешения читать другие данные или выполнять произвольный код.

Capabilities разделяются: structured extraction, bounded analysis tool planning, explanation, audio transcription, image input, streaming, JSON Schema profile и context limits. Не требовать audio у текстовой модели. Не объявлять одинаковые возможности всем endpoints, назвав их compatible.

Каждый запрос несёт destination/model instance, разрешённые категории, лимит контекста и бюджет. Пользовательские заметки, имена активностей, схемы, импортированные labels и чужие packs считаются данными, не системными инструкциями.

Смена модели — сравнение одинаковых доменных результатов на синтетических сценариях. Статистика считается кодом. Недостаток наблюдений — допустимый полезный ответ. Автоматический cloud fallback из локального режима запрещён без отдельного явного consent.

## 5. Пакеты данных, а не исполняемые плагины

Пакет трекеров содержит определения с версиями, semantic IDs полей, units/scales, разрешённые агрегаты, labels/help, предлагаемые forms и disabled initiative presets. Сложные встроенные сценарии могут иметь доверенную реализацию в приложении; импортируемый пользовательский пакет не подменяет её.

Разделять: пользователь подтвердил факт; тип данных прошёл schema validation; модель уверена в extraction; датчик что-то оценил. Эти утверждения не эквивалентны.

Пакет аналитики содержит ограниченный AnalysisSpec и правила достаточности, не arbitrary SQL или Python. Он не приобретает право читать новый класс данных при установке. Новые поля не расширяют consent старого пакета.

## 6. Discovery и доверие

Можно использовать стандартные Python entry points (E06). Discover metadata → проверить совместимость и allowlist → загрузить только разрешённый пакет → создать экземпляр из instance config. Коллизии provider ID отклоняются, а не разрешаются непредсказуемо по порядку установки.

Версия контракта отдельна от миграции БД и версии приложения. Совместимость public types проверяется в CI. Контрибьютор не должен держать форк ради каждой новой версии внутренних ORM-моделей.

Важно: allowlist и permissions в SDK не превращают in-process Python в изолированное исполнение. Доверенный плагин потенциально обладает правами процесса. Для непроверенных расширений нужен отдельный процесс/контейнер и реальные ограничения доступа; до этого не рекламировать sandbox.

## 7. Starter kit

Предложенные артефакты следующего этапа:

- `examples/source-import/`: синтетический файловый источник, preview, cursor, повторное получение и исправление.
- `examples/channel-local/`: локальный вход/ответ, text fallback, callbacks, bounded outbox.
- `examples/model-local/`: конкретный backend с явными capabilities и безопасными ошибками.
- `examples/packs/focus/`: многополевой трекер + допустимый отчёт + fixtures без личных данных.
- Команды запуска примеров, создаваемые в рамках CP-07, и единый TCK. Не выдавать эти будущие команды за существующие сейчас.

Каждый starter должен запускаться вне дерева основного пакета и не получать production secrets. README starter объясняет задачу, интерфейс, unsupported cases и как проверить изменение.

## 8. Минимальные acceptance suites

| Область | Сквозные случаи |
|---|---|
| Lifecycle | disabled/SDK absent; регистрация расширения; startup/shutdown; ошибка одного адаптера не выключает дневник |
| Источник | pagination; retry; duplicate; stale correction; partial snapshot; clock/DST; unit mismatch; identity mismatch |
| Канал | authenticate before ingest; duplicate inbound; edit/reply; action replay; expired action; ambiguous delivery; restart lease; privacy revoke |
| Модель | invalid JSON; ошибочная доза/дата; неизвестный metric ID; неподдержанный tool; budget; timeout/429; отказ consent; false citation |
| Факты | needs_confirmation не observed; неизвестное не ноль; ordinal scales не смешиваются; открытые интервалы пересекают сутки |
| Экспорт | определения без фактов; raw/credentials не попадают в sharing; backup restore сохраняет версии, связи, согласия и dedup |
| Пользователь | create custom tracker → write → correct → compare → pause → restart через реальные входы выбранных интерфейсов |

Разделять уровни evidence: unit; реальная тестовая БД; сквозной локальный transport; live provider; эксплуатационное наблюдение. Один уровень не подменяет следующий.
