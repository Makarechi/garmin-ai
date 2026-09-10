# Analytic conversation context (GA-14, first phase)

Telegram analytic answers retain up to six recent question/answer snippets and the
successful tools' bounded arguments/result hashes in local `AppState`. The context
is capped at 12,000 UTF-8 bytes and seven days. It survives restarts and is included
in the shared analytical prompt budget. Snippets and omitted tool specs are marked
as truncated. Raw health result payloads are not copied into this conversation store.

A reply to a delivered bot message selects that original question through the durable
outbox message mapping. An unknown/expired/evicted reply never silently selects a newer
topic: the user is asked to repeat the original question and period. New questions may
use recent conversation for explicit follow-ups; old answers are marked non-authoritative
and current tool evidence is still required. Past relative dates retain the original
question timestamp; new relative dates use the new message timestamp.

`/conversation` shows retained question snippets. `/forget_conversation` clears this
context without altering diary events. An epoch fence prevents an in-flight answer from
recreating context after an explicit forget. Future conversations may start normally.
Ordinary diary parsing receives only the latest analytic topic, never target IDs from
analytic memory. Conversation text and tool results remain untrusted prompt data.

The snippets are conversation context, not a complete reproducible `AnalysisSpec` or
archival evidence. Explicit long-term preferences, exact historical evidence inspection,
structured filter editing and provider-level follow-up quality evaluation are later phases.
Synthetic tests verify persistence, reply mapping, fresh tool calls, bounded retention,
forget races and separation from diary mutation targets. No new live model call was made.
