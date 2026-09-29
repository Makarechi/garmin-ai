# HF-12: статус GA и отдельная эксплуатационная приёмка

Срез статуса: 2026-09-24. Подробные исходные критерии GA-01…GA-32 находятся в
[implementation-backlog.md](implementation-backlog.md). Таблица ниже сверяет эти критерии с
уже слитыми PR; факт слияния означает наличие кода, а не закрытие всей задачи.
«Синтетически принят» означает автоматические проверки ограниченного маршрута на тестовых
данных. «Live pending» означает отсутствие новой проверки реального аккаунта, устройства или
сервиса после универсального релиз-кандидата. Семидневный прогон и off-host restore не
выполнены этим этапом. Состояние общего release gate зависит от [HF-11](verification.md).

| Задача | Статус кода и синтетики | Остаток по критериям |
| --- | --- | --- |
| GA-01 | Синтетически принят ограниченный interval route ([#12](https://github.com/Makarechi/garmin-ai/pull/12), HF-05) | Проверить на личной истории после миграции; live pending. |
| GA-02 | Синтетически принят контрольный период ([#13](https://github.com/Makarechi/garmin-ai/pull/13), HF-04/05) | Реальный longitudinal outcome и пересчёт после исправлений; live pending. |
| GA-03 | Синтетически принят контракт freshness/coverage ([#14](https://github.com/Makarechi/garmin-ai/pull/14), [#74](https://github.com/Makarechi/garmin-ai/pull/74)) | Проверка каждого фактического Garmin endpoint и SLO; live pending. |
| GA-04 | Синтетически принят pre-event cutoff ([#17](https://github.com/Makarechi/garmin-ai/pull/17)) | Полнота версий и реальное покрытие исторических наблюдений; live pending. |
| GA-05 | Синтетически принят типизированный агрегатор ([#18](https://github.com/Makarechi/garmin-ai/pull/18), HF-06) | Подтвердить единицы и interval semantics каждого реального источника; live pending. |
| GA-06 | Код и синтетика частичные ([#19](https://github.com/Makarechi/garmin-ai/pull/19), [#21](https://github.com/Makarechi/garmin-ai/pull/21), [#71](https://github.com/Makarechi/garmin-ai/pull/71)) | Account-export ZIP и полный исторический цикл; live pending. |
| GA-07 | Код и синтетика частичные ([#20](https://github.com/Makarechi/garmin-ai/pull/20)) | Вход без остановки, поколение токенов, постоянные ошибки endpoint; live pending. |
| GA-08 | Код и синтетика частичные ([#23](https://github.com/Makarechi/garmin-ai/pull/23)) | Реальные capabilities, schema drift/quarantine, все версии архива; live pending. |
| GA-09 | Код и синтетика частичные ([#25](https://github.com/Makarechi/garmin-ai/pull/25)) | Полнота в реальных адаптерах и полный цикл удаления; live pending. |
| GA-10 | Код и синтетика частичные ([#22](https://github.com/Makarechi/garmin-ai/pull/22)) | Activity samples, спортивные features, адаптивное обновление; live pending. |
| GA-11 | Код и синтетика частичные ([#27](https://github.com/Makarechi/garmin-ai/pull/27)) | Naps/stage intervals, Body Battery и load с реальным покрытием; live pending. |
| GA-12 | Код и синтетика частичные ([#48](https://github.com/Makarechi/garmin-ai/pull/48)) | Межтренировочная идентичность и атрибуция сенсоров; live pending. |
| GA-13 | Код и синтетика частичные ([#29](https://github.com/Makarechi/garmin-ai/pull/29), [#31](https://github.com/Makarechi/garmin-ai/pull/31), [#53](https://github.com/Makarechi/garmin-ai/pull/53), [#59](https://github.com/Makarechi/garmin-ai/pull/59)) | Неточные границы, исходы, редактор пресетов, справочник лекарств. |
| GA-14 | Код и синтетика частичные ([#36](https://github.com/Makarechi/garmin-ai/pull/36)) | Полный AnalysisSpec, воспроизводимость и качество диалога; live pending. |
| GA-15 | Код и синтетика частичные ([#28](https://github.com/Makarechi/garmin-ai/pull/28)) | Качество свободного текста на реальном провайдере; live pending. |
| GA-16 | Код и синтетика частичные ([#46](https://github.com/Makarechi/garmin-ai/pull/46), [#47](https://github.com/Makarechi/garmin-ai/pull/47)) | Redo, редактированные сообщения и дополнительные голосовые сценарии. |
| GA-17 | Код и синтетика частичные ([#30](https://github.com/Makarechi/garmin-ai/pull/30), HF-02) | Первый check-in, выбор тем, snooze и частота; live pending. |
| GA-18 | Код и синтетика частичные ([#26](https://github.com/Makarechi/garmin-ai/pull/26)) | Визуальная физиология и baseline по времени суток. |
| GA-19 | Код и синтетика частичные ([#45](https://github.com/Makarechi/garmin-ai/pull/45)) | Долгий feature store, другие исходы, репликация и multiplicity. |
| GA-20 | Код и синтетика частичные ([#44](https://github.com/Makarechi/garmin-ai/pull/44)) | Поправки на маршрут/условия/сенсоры и динамика формы. |
| GA-21 | Код и синтетика частичные ([#49](https://github.com/Makarechi/garmin-ai/pull/49)) | Telegram workflow, вмешательства, adherence и sensitivity analysis. |
| GA-22 | Код и синтетика частичные ([#37](https://github.com/Makarechi/garmin-ai/pull/37), [#61](https://github.com/Makarechi/garmin-ai/pull/61), [#62](https://github.com/Makarechi/garmin-ai/pull/62)) | Check-in UI, расписание, outcome у insights. |
| GA-23 | Код и синтетика частичные ([#33](https://github.com/Makarechi/garmin-ai/pull/33), [#43](https://github.com/Makarechi/garmin-ai/pull/43)) | Долговременный AnalysisRun, cost budget, шаблоны качественных claims. |
| GA-24 | Код и синтетика частичные ([#35](https://github.com/Makarechi/garmin-ai/pull/35), HF-01) | Второй backend, условия конкретного проекта провайдера и privacy UI; live pending. |
| GA-25 | Код и синтетика частичные ([#38](https://github.com/Makarechi/garmin-ai/pull/38), [#67](https://github.com/Makarechi/garmin-ai/pull/67), UNI-15) | Полный wizard и повторное развёртывание на независимом host; live pending. |
| GA-26 | Синтетически принят scoped owner/API/MCP guard ([#15](https://github.com/Makarechi/garmin-ai/pull/15), [#16](https://github.com/Makarechi/garmin-ai/pull/16), HF-01) | Повторить после off-host restore и смены owner binding; live pending. |
| GA-27 | Код и синтетика частичные ([#41](https://github.com/Makarechi/garmin-ai/pull/41), [#56](https://github.com/Makarechi/garmin-ai/pull/56), [#58](https://github.com/Makarechi/garmin-ai/pull/58), [#64](https://github.com/Makarechi/garmin-ai/pull/64), [#65](https://github.com/Makarechi/garmin-ai/pull/65), [#68](https://github.com/Makarechi/garmin-ai/pull/68)) | Off-host drill, общий retention, многолетняя нагрузка; operational pending. |
| GA-28 | Код и синтетика частичные ([#34](https://github.com/Makarechi/garmin-ai/pull/34), [#57](https://github.com/Makarechi/garmin-ai/pull/57), [#63](https://github.com/Makarechi/garmin-ai/pull/63), [#70](https://github.com/Makarechi/garmin-ai/pull/70), HF-11) | Evals/chaos и 7 суток наблюдений; operational pending. |
| GA-29 | Код и синтетика частичные ([#39](https://github.com/Makarechi/garmin-ai/pull/39), [#69](https://github.com/Makarechi/garmin-ai/pull/69)) | Редактор панели, heatmap, карточки evidence. |
| GA-30 | Код и синтетика частичные ([#51](https://github.com/Makarechi/garmin-ai/pull/51)) | Провайдеры погоды/календаря и история местоположения; подключения live pending. |
| GA-31 | Серверный контракт синтетически проверен ([#54](https://github.com/Makarechi/garmin-ai/pull/54)) | Клиент и matrix устройства намеренно отложены до решения владельца; live pending. |
| GA-32 | Код и синтетика частичные ([#40](https://github.com/Makarechi/garmin-ai/pull/40), [#52](https://github.com/Makarechi/garmin-ai/pull/52)) | Справочник и управление необработанными сообщениями; live provider pending. |

## Операционные проверки: отдельный протокол

1. **До запуска.** Зафиксировать release SHA, схему, список включённых endpoints,
   согласованные SLO и расписание; сделать проверенный backup и baseline counts по
   definitions, versions, events, observations, consent и audit. Переносить приватный
   отчёт и backup только по согласованному владельцем каналу. Новые live-вызовы,
   deployment, off-host transfer и чтение личных данных требуют отдельного решения.
2. **Семь суток без оператора.** Каждые 5 минут фиксировать timestamp, доступность
   API/worker, последний успешный fetch и последний observed timestamp по endpoint,
   возраст очередей и send intents, retry/rate-limit/reauth, backup age и свободное
   место. Хранить причины пробелов; потерю наблюдения не считать нулевой ошибкой.
   Ежедневно сверять долю интервалов с данными, задержки p50/p95/max, дубли
   операций/доставок, неподтверждённые и uncertain отправки, срабатывания и доставку
   alert. Любое вмешательство оператора помечать с временем и перезапускать окно,
   если оно устранило отказ. Дату merge нельзя считать началом наблюдения.
3. **Сбой и восстановление.** В синтетической среде повторить выключение worker,
   разрыв БД, 429 и неопределённую доставку. После restart проверить fencing,
   повторный claim, отсутствие дублирования фактов, лекарств и confirmed send,
   сохранение pause/consent, метрик задержки и audit. Сравнить с зафиксированными
   до сбоя ключами идемпотентности.
4. **Off-host restore.** На отдельном изолированном host с чистой БД и отключёнными
   внешними отправками проверить криптографическую целостность backup, восстановить
   подходящим бинарником и миграциями, сопоставить counts и хеши обезличенных
   идентификаторов/связей для definitions, versions, entries, observations,
   confirmation/cutoff, audit и destination consent. Проверить чтение и локальную
   запись после restart, ownership и запрет старой исходящей очереди. Секреты из
   backup не публиковать; test recipient не должен получить старое сообщение.
   Старый бинарник на новую схему не запускать: откат через проверенный снимок
   до cutover либо совместимый forward fix.
5. **Device/provider contracts.** Для конкретной модели часов/ОС и каждого
   используемого Garmin endpoint записать доступность, схему, единицы,
   частоту/давность, пограничные и пустые ответы. Отдельно проверить реальные
   разрешения, тариф/ограничения и retention модели и канала. Эти проверки
   проводить лишь после решения владельца и хранить account-specific выводы
   приватно. Публичный итог содержит только версию, дату, обезличенные статусы
   и ограничения.

## Выходные артефакты и решение

Отчёт должен содержать SHA/схему, команды и конфигурацию без секретов, непрерывность
семидневной выборки, метрики по каждому endpoint и классу очереди, перечень
инцидентов/вмешательств, сверку off-host restore, матрицу device/provider и
отдельный список неисполненных live-проверок. Владелец выбирает SLO и
разрешает реальные операции до их запуска. До появления этих артефактов
статус HF-12 остаётся «план подготовлен; эксплуатационная приёмка pending».

Дополнительные Garmin/аналитические возможности выбирать после стабилизации
универсальных маршрутов. Автоматический диагноз/лечение и общий health score
не входят в этот план.
