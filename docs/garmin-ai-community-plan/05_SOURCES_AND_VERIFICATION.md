# Источники и границы проверки

Дата: 2026-10-04.

## Что выполнено

Через подключённый GitHub прочитаны main, metadata, корневое и вложенные деревья, Releases, README, CONTRIBUTING, package/Compose/CI, архитектура и часть ключевых реализаций registry/runtime/onboarding/settings/Garmin и core-only test. Проверено состояние check runs указанного SHA. Публичные спецификации прочитаны отдельно через web.

**Тесты не перезапускались; CI-artifacts с числом тестов в этом обзоре не скачивались.** Поэтому здесь нет нового утверждения о количестве прошедших тестов. Нет локального production clone, доступа к health rows, live login, API quota, deploy или изменений GitHub.

Один запрос к предполагаемому `docs/tracker-forms.md` вернул 404; это не использовано как доказательство отсутствия документации о трекерах. Поиск GitHub по слову tracker не вернул результатов; отрицательный результат не использован для вывода об отсутствии реализации. Часть больших tree/docs-ответов была усечена; выводы о коде сделаны по отдельным fetch_file, а не по отсутствию пути в усечённом тексте.

Публичные страницы Meta Developer Documentation не удалось полноценно прочитать; правила WhatsApp взяты из отдельно открытой Business Messaging Policy. Нерелевантные поисковые результаты не использованы.

## Источники репозитория

### R01 — Metadata репозитория

https://api.github.com/repos/Makarechi/garmin-ai

license=null; topics=[]; has_discussions=false; open_issues_count=0. Снимок чтения, не неизменяемое состояние.

### R02 — README

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/README.md

Позиционирование, setup, privacy, заявленные capabilities.

### R03 — Корневое дерево

https://api.github.com/repos/Makarechi/garmin-ai/git/trees/989c520e9e5de2c67340590dd543a8f6de00ffa6

Нет LICENSE в корне. Не заменяет полный анализ авторских прав.

### R04 — Channel adapter guide

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/docs/channel-adapter-guide.md

Текущий контракт и reference adapter.

### R05 — Архитектура

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/docs/architecture.md

Документированное устройство. Проверять код, не трактовать текст как независимую приёмку.

### R06 — Integration registry

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/src/garmin_ai/integrations.py

Реестр kind/factory; built-ins Garmin, Telegram, Gemini; глобальные settings factories.

### R07 — Runtime

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/src/garmin_ai/runtime.py#L445-L670

Явный выбор конкретных providers и Telegram delivery ветки.

### R08 — Settings

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/src/garmin_ai/config.py#L1-L240

IntegrationInstance, optional settings, consent, locale, named instances.

### R09 — Package metadata

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/pyproject.toml

Зависимости/extras; проектная версия 0.1.0; лицензия не объявлена в прочитанном manifest.

### R10 — Onboarding

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/src/garmin_ai/onboarding.py#L1-L240

OnboardingPlan, выбор пакетов, sources/channels/model categories и manifest import.

### R11 — Dashboard guide

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/docs/data-dashboard.md

Синтетический demo уже существует; guide описывает первую поставку и может отставать от UI.

### R12 — Core-only flow test

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/tests/test_core_only_flow.py

Существующий DB-backed тест: многополевой tracker с event_count, запись, правка, repeat read. Не запускался в этом обзоре.

### R13 — Compose

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/compose.yml

Локальная сборка, image :local, per-instance ports и paths.

### R14 — GitHub Releases

https://api.github.com/repos/Makarechi/garmin-ai/releases?per_page=10

На момент чтения пустой список. Это не проверка существования любых сторонних сборок.

### R15 — CONTRIBUTING

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/CONTRIBUTING.md

Agent-centric workflow и правила synthetic fixtures.

### R16 — Check runs

https://api.github.com/repos/Makarechi/garmin-ai/commits/8d9bd0208da5891f836ce7045004ef729fa40aab/check-runs

checks и core-only completed/success. Run ID 36942021070.

### R17 — CI workflow

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/.github/workflows/ci.yml

Обе конфигурации используют отдельную TimescaleDB; артефакты и skip reasons.

### R18 — Operational acceptance

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/docs/operational-acceptance.md

Статусные ограничения и проектный протокол seven-day/off-host/live checks.

### R19 — Implementation backlog

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/docs/implementation-backlog.md

Старый GA backlog остаётся частичным; универсальность не закрывает всё.

### R20 — Garmin adapter

https://github.com/Makarechi/garmin-ai/blob/8d9bd0208da5891f836ce7045004ef729fa40aab/src/garmin_ai/garmin.py

Текущая библиотека, read-only registry, identity и retry/rate limit.

### R21 — Проверка main

https://api.github.com/repos/Makarechi/garmin-ai/branches/main

HEAD определён connector read, commit date 2026-10-01T23:40:10Z.

## Внешние первичные источники

### E01 — GitHub: Licensing a repository

https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository

Публичный репозиторий не заменяет явную open-source лицензию. Это общая документация, не legal audit данного проекта.

### E02 — Apache License 2.0

https://www.apache.org/licenses/LICENSE-2.0

Основание предлагаемого выбора: права переиспользования, условия распространения и patent grant. Право выбрать лицензию не проверялось.

### E03 — WhatsApp Business Messaging Policy

https://business.whatsapp.com/policy

Открыта политика с датой 2026-09-23; redirect на whatsappbusiness.com. Условия customer-service window, templates, opt-in и health-related data. Допустимость конкретного AI/deployment не установлена.

### E04 — Viber Developers Hub

https://developers.viber.com/

Коммерческие условия создания новых bots с 2024-02-05; без проверки конкретной стоимости или регистрации.

### E05 — Ollama structured outputs

https://docs.ollama.com/capabilities/structured-outputs

Локальный JSON-schema structured output. Не доказательство качества конкретной модели или transcription; отдельно отмечено отсутствие такой поддержки у Ollama Cloud на момент чтения.

### E06 — PyPA entry-points specification

https://packaging.python.org/en/latest/specifications/entry-points/

Стандарт metadata/discovery установленных extensions; не sandbox и не готовая интеграция в Garmin AI.

### E07 — Garmin Health API overview

https://developer.garmin.com/gc-developer-program/health-api/

Данные доступны после sync устройства с Garmin Connect; официальный API требует отдельного approval. Это не описание текущего неофициального адаптера проекта.

## Как читать рекомендации

CP-01…CP-12, будущие пути `examples/`, proposed SDK, demo-flow, web-chat и локальный второй backend — предложения, а не существующие функции. Числа пилота и пороги времени — выбранные цели, не статистика рынка и не измерения проекта.

Этот пакет не закрывает GA/UNI/HF и не утверждает, что все старые баги исправлены. Обязателен отдельный release-readiness проход по текущим regression cases, schema, consent/pause и эксплуатации.
