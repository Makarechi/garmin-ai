# Model instruction language

The instructions sent to the model are bundled as reviewable text files in
`src/garmin_ai/prompts/`. The diary, analysis, safety, generated-tracker,
transcription and MCP routes load them through one fixed catalog. User messages,
saved records and tool results cannot select another instruction file.

The instructions use English as a neutral control language. They require the
model to understand the **current owner message** in any language, set
`response_language` from that message when the response schema supports it,
and write clarifications or answers in the same language. The configured UI
locale remains a fallback when the model does not identify the language; it is
not a restriction on the message language. Voice transcription preserves the
spoken language rather than translating it.

Changing a prompt is a behavior change: review the domain rules, run the
Russian and English synthetic regression cases, and evaluate the selected
real model with fictional messages before claiming conversational quality.
The ordinary CI checks routing, schema validation and deterministic safety
guards. It cannot prove how an external model interprets every language.
Provider-backed tests are intentionally opt-in and are not part of a supported
release claim.
