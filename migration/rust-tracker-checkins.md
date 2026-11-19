# R30 scheduled tracker check-ins

The worker projects enabled tracker reminders into the shared rule records. It
uses the current owner, selected conversation and tracker definition. It
preserves the rule ID and computes the same rule revision as Python, so a
runtime switch can reuse the saved reminder. The projection retains eligible
fallback destinations and the owner's existing enablement, consent and snooze.

Native delivery currently covers tracker reminders whose primary channel is
the paired Telegram account. It waits until the incoming Telegram queue has
drained. The normal worker can run these reminders without a model. A saved
entry, a withdrawn sharing grant, a pause or an edited rule can prevent a send
after the reminder has already been queued. Quiet hours and the daily budget
use the shared policy. An occurrence that may already have been sent consumes
that day's slot. A clear provider refusal can be retried.

The frozen Python suite passed 82 initiative scenarios. The paired Linux ARM64
database fixture matched saved rules, consent checks, reminder text, the
occurrence and its retry time over an autumn clock change. It also checked
a pause after claim, a replay lock, and an entry saved after claim. A loopback
Telegram test covered a clear refusal followed by a connection loss. The
normal Linux worker waited for the webhook backlog, sent one reminder without
a configured model and confirmed its state after a restart. All databases
contained synthetic data. The isolated worker fixture reached 18,694,144 bytes
under a 48 MiB limit with zero swap and zero OOM events.

Remaining R30 work covers channel fallback, other rule types and the combined
failure and recovery workload. A live bot was outside these checks.
