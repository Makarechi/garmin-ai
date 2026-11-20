# R28 owner web chat model processing

The owner route keeps the same request, history and read confirmation contract.
Local commands and urgent text are handled before model selection. When diary
processing is allowed, the server reserves the client message ID and reads the
authorized context. This reservation uses the dedicated model pool. The model
request runs between short storage operations.

The server checks the current model and channel permissions and the context
used by the model before publishing a diary entry or clarification. A fixed
safety notice uses only the current owner conversation and carries no diary
references. Diary entries, clarifications and replies use the same transaction
as the inbound message.
A failed save rolls back its attempted changes before producing the owner reply.
Retries use the original reply, and reusing an ID for different text returns 409.

The frozen Python web chat tests passed all 16 cases. The paired HTTP fixture ran
the native Linux ARM64 executable against a loopback HTTPS model. It compared
the owner response for a diary entry, a clarification, a safety message,
withdrawn consent and an upstream failure. Python and Rust reused one committed
reply across the runtime boundary. The native test confirmed a read receipt
after fetching the message. During a held model request, an unrelated local
command completed and a duplicate shared the original reply. Revoking diary
consent while a synthetic response was in flight prevented the proposed write.

The catalog checks model and channel grants when showing tracker names and
record counts. Before displaying an answer, both web senders require channel
access to schema and facts for the consulted tracker definitions. The paired
fixture covers schema-only grants, a page that consults a tracker beyond the
returned rows, permitted counts and the withdrawal of access to a saved reply.
The native runtime rechecks the catalog evidence before subsequent model calls
and delivery.
A separate run cancelled a pending clarification from another tab during a
safety classification. The safety notice reached the owner and was reused
after reconnecting through the Python server with the same message ID.

The same paired fixture covers a single-target note correction and closure
of a confirmed open episode. Both channels use the same revision and field
validation. In the web fixture, both runtimes preserved original text,
wrote one audit revision with the matching inbound operation and returned
the same reply. A duplicate reused that reply and an unoffered target
produced a local refusal. The native Telegram stale-revision regression
also passed on Linux ARM64 under a 48 MiB limit.

Natural-language undo uses the web conversation's latest recorded operation.
The paired fixture matched the Python response and audit revision. It also
verified that a duplicate request reuses the original response and a delayed
model decision cannot undo a newer entry from another tab.

The ordinary model flow can include a localized description of the latest
stored Garmin observations and daily summaries. The model receives only the
channels permitted by the owner's current categories and packs. A replay
in progress withholds dependent channels and keeps the recalculation notice.
The server rechecks the underlying report and permissions before showing or
acknowledging a stored answer. The paired web fixture exercised both permission
revocation and replay after drafting, and matched the frozen Python replies.

The remaining analytical tools, corrections with linked additional entries,
and combined browser flows remain release work.
