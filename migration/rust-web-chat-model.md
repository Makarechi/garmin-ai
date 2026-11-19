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

Analysis, corrections with linked additional entries and combined browser
flows remain release work.
