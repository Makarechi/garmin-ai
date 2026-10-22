# Natural-language tracker input

Natural-language input is an optional adapter over the generated tracker forms. It cannot create
new field identities, choose a hidden tracker, bypass permissions or weaken validation.

The interpreter receives at most five active tracker contracts. Labels and the owner message are
marked as untrusted data. Its structured result names the immutable definition version and stable
field IDs, and every extracted value or changed time points to an exact character range in the
source message. The application verifies those ranges before passing the result through the same
form service used by the dashboard.

Setup wishes and facts are separate operations. “I want to track stretching” can only return a
tracker preview with the existing explicit confirmation token; it cannot create a stretching
entry. Definition changes are also returned as proposals and are never activated by the model.
An entry correction requires an explicitly selected event and preserves unmentioned values and
times from that immutable version.

The channel, actor and permissions come from trusted application code, not from the message or
model. Provider output outside the bounded tracker and field IDs is rejected. Quantity values need
literal unit evidence, interval endpoints need literal clock evidence, and the generated schema
performs the final bounds and required-field checks. Stored entries retain the original text and
only source offsets as evidence references.

## Calendar evidence (MA-01)

Every changed timestamp needs an independently supported calendar day as well as
a clock. Its exact source quote must contain an ISO date (`2026-10-22`), a
day/month/year date (`22.10.2026` or `22/10/2026`), or a supported English/Russian
`today`/`yesterday`/`tomorrow` expression. `now` and its supported Russian equivalents
use the original message instant, with the existing two-minute tolerance measured
as elapsed time. Calendar arithmetic uses the original message timezone.

**Intentional contract change:** a clock alone, including an otherwise valid
English/Russian `at 14:00` or `в 14:00`, no longer defaults to today. The model must
ask for a day. A clipped quote such as `14:00` cannot borrow a day from another
part of the message. Interval endpoints each require complete temporal evidence;
an unchanged timestamp on a selected entry is preserved without new evidence.
Deterministic forms remain available to specify a date directly.

The validator distinguishes `supported`, `ambiguous`, `unverified`, and
`contradictory` evidence, and records the supported basis as `explicit_date`,
`relative_day`, or `message_time`. Non-supported temporal proposals return HTTP
200 with `intent=clarify`, `written=false`, a clarification, and `temporal_evidence`
containing the field, status and reason. They never reach the fact-writing service
and leave no event, metric projection, timeline interval, or create/update audit.
Malformed source ranges and other schema, permission or quantity errors keep their
existing rejection behavior.

First model attempts persist only the request hash, original message time and
timezone under the actor-scoped operation identity. Clarification or provider
failure does not cache a model answer. Retrying the same request after midnight,
restart or a profile timezone change reuses that context; a different request
cannot reuse the identity. Completed operations still return their first receipt.
Clients must use a new operation ID for a new clarification message.

Repeated DST clocks require a matching explicit UTC offset attached to the clock.
Nonexistent local clocks, conflicting days, ambiguous reuse of one clock across
different days, and approximate English/Russian times require clarification. These
checks do not establish support for other languages: Slovak `včera` and Spanish
`ayer` remain unverified, including when the proposed date happens to be correct.
No language whitelist or medication/consent guard has been removed.

Synthetic regressions are in `tests/test_tracker_temporal_safety.py`. They exercise
the helper, actual write service and authenticated HTTP entry with disposable
PostgreSQL/TimescaleDB and a synthetic model. They do not evaluate a live model or
claim that the counterexample occurred in a real diary. Linked-batch rollback is
checked separately in `tests/test_event_batches.py`; this endpoint writes one
tracker entry per operation and does not introduce a tracker batch API.

If the configured model is unavailable, the endpoint returns the deterministic generated form (or
the tracker builder for a caller allowed to manage definitions). No fact is dropped or guessed,
and deterministic forms remain fully usable without a network model.
