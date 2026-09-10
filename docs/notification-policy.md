# GA-17: shared proactive quota

Completion criteria: an answered coffee question does not suppress a week of follow-ups; questions and insights share the configured daily limit; reservations survive restart and allow retrying the same rate-limited delivery; pause, quiet hours and pending diary clarification apply to both paths.

`question_budget` now limits combined proactive questions and insight notices per local calendar day. Question `sent_at` and persistent insight reservations consume slots, including uncertain delivery. A shared advisory transaction lock serializes reservation. Retrying the same insight reuses its slot; other notices cannot bypass the budget. Technical authentication and API quota notices remain outside this proactive quota.

Reservations are conservative: an interrupted send may occupy a slot until the day ends. Existing per-category question cooldown and seven-day insight metric cooldown remain in effect. Answered and acknowledged caffeine questions no longer count as ignored; sent or uncertain unanswered questions still suppress repeated prompts.

Validation uses synthetic database tests for mixed question/insight quotas, restart, retry, zero budget, pause and response statuses, plus the existing proactive suite. No live Telegram requests were sent. Cold-start check-ins, user-selected topics, snooze and decline preferences remain later GA-17 slices.
