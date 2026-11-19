# R28 owner web chat model processing

The owner route keeps the same request, history and read confirmation contract.
Local commands and urgent text are handled before model selection. When diary
processing is allowed, the server reserves the client message ID and reads the
authorized context. This reservation uses the dedicated model pool. The model
request runs between short storage operations.

The server checks the current model and channel permissions and the context
used by the model before publishing a reply. Model-assisted diary entries,
clarifications and replies use the same transaction as the inbound message.
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

The tested model flows are new entries, clarifications and safety replies.
Corrections, analysis and combined browser flows remain release work.
