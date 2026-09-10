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

Only turns with a confirmed sent Telegram outbox part enter context. Pending or uncertain delivery stays hidden. The runtime scheduler purges expired context every thirty seconds while running; reads also prune it. Oversized escaped text is omitted if removing specifications cannot satisfy the full stored-value byte cap. Forgetting uses the control queue and bypasses older delayed analysis jobs.

Undelivered output is staged separately as one pending turn, bounded to 12,000 bytes and seven days. It cannot evict the six delivered turns (whose independent 12,000-byte cap remains). Context reads and scheduler ticks promote it only after a sent outbox confirmation. A later undelivered answer replaces only the pending slot. Forget removes both stores and preserves the epoch fence.

Replies to known analytical answers are resolved before free-text intent classification. Their outbox marker survives snippet expiry, so unavailable analytic context asks for the original question. Replies to diary or proactive messages retain ordinary intent routing. Urgent safety screening also runs for queued replies and expired analytical context. A dedicated control worker processes forgetting while the ordinary analysis worker waits for the provider. Forgetting changes an epoch checked before each subsequent model request and after its result; final answer persistence is serialized with forgetting. A request already sent to the provider cannot be recalled, but its stale result is discarded.
