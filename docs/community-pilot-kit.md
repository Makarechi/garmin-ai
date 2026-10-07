# Community pilot kit (preparation, not a pilot result)

This kit is ready to share **only when the selected path is ready**. The
account-free synthetic demo can be tried without a personal installation. A
self-hosted pilot needs a tested image/platform, a verified backup and the
operational gates in [operational acceptance](operational-acceptance.md).
Neither seven days of operation nor independent-host restore has been observed
for the current release candidate. Do not describe it as supported on the
basis of this kit.

## Participant invitation draft

> We are testing whether Garmin AI helps people keep a private activity and
> diary history they can correct and question. You may first use the fictional
> demo. If you choose a personal installation, it runs on your own host and
> you control optional Garmin, Telegram and model connections. We will ask
> about setup effort, confusing steps and whether the result is useful; we do
> not need your passwords, tokens, diary entries, health series or raw logs.
> Participation is optional and you can stop at any time. Please say whether
> you can try the demo only or an independent self-hosted installation.

Send this only to people who opt in through a venue where such invitations are
allowed. Do not mass message, offer medical claims or imply Garmin affiliation.
Adapt the wording to the group before posting. A public announcement should
link the exact candidate version, supported platforms and current limitations.

## Day 0: same tasks for every participant

1. Record the candidate SHA, image digest, platform and whether the person
   chose demo or self-hosting. Record prior Docker/Python experience in broad
   bands so setup friction is interpreted fairly.
2. Ask the person to follow the published instructions without an organizer
   driving the keyboard. In the demo, create a fictional tracker, enter a
   fictional fact, correct it and inspect the changed analysis.
3. For self-hosting, ask whether setup completed without editing source, which
   optional connections they enabled, whether they reached a useful first
   result and whether backup/status instructions were understandable. Do not
   request access to the instance or inspect personal records.
4. Record the time from opening the guide to the first useful result, help
   requested and the exact step where the participant stopped. An unresolved
   error is an obstacle, not a completed installation.

## Day 7: private follow-up for participants who opted in

Ask whether they used the saved history or questions again, whether initiative
messages were helpful, whether they know how to correct/export/disable AI,
what broke, and why they stopped if they did. Offer "prefer not to answer" for
each question. Do not collect diary content or source screenshots. Mark missing
follow-up as missing, not as success or failure.

## Obstacle log template

Keep one row per obstacle, in a private tracker with restricted access. A row
contains only: pilot ID (random), broad platform, candidate SHA, demo/self-hosted
path, stage, time rounded to the hour, expected outcome, observed error
category, assistance required, result, synthetic reproduction steps, owner and
resolution PR. Strip file paths, addresses, IDs, tokens, free-text diary content
and timestamps of actual health events before sharing a report. Obtain separate
permission before using any participant-provided diagnostic artifact.

## Public summary template

Report the number invited, number who opted in, day-0 and day-7 responses,
completion count, median/range time to first result where measured, recurring
obstacle categories, fixes with PR links, withdrawals and missing observations.
List the tested SHA/platforms and the remaining unsupported paths. Suppress
small or identifying slices. Do not publish raw responses, source data or
screenshots from personal installations. Two independent contribution paths
need human review before claiming the contributor goal was met.

The target is 5–10 volunteers and 2–3 potential contributors. These are pilot
recruitment goals, not an achieved sample or a statistical claim. The next
roadmap decision should follow repeated obstacles and the independent
operational evidence, not a single anecdote.

## Кратко по-русски

Сначала предложите добровольцам демо только на вымышленных данных. Личную
установку предлагайте лишь для проверенного образа и платформы, с понятным
резервным копированием. Не просите пароли, исходные ряды здоровья или доступ к
экземпляру. На первый день измеряйте путь до полезного результата и шаги,
где потребовалась помощь; на седьмой — повторное использование и причины
отказа. Отсутствующий ответ остаётся отсутствующим. Публично публикуйте
только агрегированные результаты и ограничения, без личных записей.
