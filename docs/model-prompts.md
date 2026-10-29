# Model instruction language

The instructions sent to the model are bundled as reviewable text files in
`src/garmin_ai/prompts/`. The diary, analysis, safety, generated-tracker,
transcription, extension capability checks and MCP routes load them through one fixed catalog. User messages,
saved records and tool results cannot select another instruction file.

The instructions use English as a neutral control language. They ask the model
to use an explicitly requested response language, or the current owner message's
language when there is no such request. For a short, ambiguous continuation it
may use relevant owner conversation context, but not the language of stored
records or tool output. When the response schema supports it, the model returns
the choice in `response_language`. The configured UI locale remains a fallback
when the model cannot determine a language. Voice transcription preserves the
spoken language rather than translating it. These instructions do not themselves
prove that a particular model handles multilingual requests correctly.

The generated-tracker entry path currently validates English and Russian time
and unit expressions. Its instruction asks for clarification when another
language cannot be independently evidenced. This is a boundary of the current
implementation, not a claim that other languages are supported end to end.

Changing a prompt is a behavior change: review the domain rules, run the
Russian and English synthetic regression cases, and evaluate the selected
real model with fictional messages before claiming conversational quality.
The ordinary CI checks routing, schema validation and deterministic safety
guards. It cannot prove how an external model interprets every language.
Provider-backed tests are intentionally opt-in and are not part of a supported
release claim.
