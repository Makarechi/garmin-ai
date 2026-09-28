# Overnight check-in window

New initiative intents carry `scheduled_day` and `logical_notification_id` as typed fields; the outbox dedup key remains a storage identity. Pre-existing intents without these fields remain readable through the legacy dedup date fallback.

Scheduled and missing-entry questions normally expire at the end of their local scheduled day. When quiet hours or a short recovery after midnight moves a question to the next day, it can carry for at most 12 hours from its scheduled local time. A deferral beyond that bound creates an `initiative:skip:<rule-id>:<scheduled-day>` state record with a reason and queues no send attempt. The original scheduled day continues to select the evidence day for a missing-entry question.

This slice covers overnight carry and skip for daily check-ins. Fallback conversation binding, provider receipt semantics, and DST handling remain separate HF-03 work.
