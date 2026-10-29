# Model instruction language

The instructions sent to the model are bundled as reviewable text files in
`src/garmin_ai/prompts/`. The diary, analysis, safety, generated-tracker,
transcription, extension capability checks and MCP routes load them through one fixed catalog. User messages,
saved records and tool results cannot select another instruction file.

The instructions use English as a neutral control language. They require the
model to identify the language of the **current owner message**, set
`response_language` from that message when the response schema supports it,
and write clarifications or answers in the same language. The configured UI
locale remains a fallback when the model does not identify the language; it is
not a restriction on the message language. Voice transcription preserves the
spoken language rather than translating it.

Settings, response language tags, generated tracker labels and action/form locale
parameters use the same well-formed BCP 47 syntax and conventional casing. They
accept scripts and numeric regions (for example, `zh-Hans`, `sr-Latn-RS`, and
`es-419`); the check does not consult the IANA registry or establish that a
language is translated or supported end to end. Interface message catalogs remain
English and Russian; other languages use the English catalog fallback. Onboarding
still offers only those two interface languages.
For user-defined tracker labels, lookup first tries the full tag, then progressively
less-specific tags (for example, `zh-Hant-TW` can use `zh-Hant`), before using an
English or existing label. This does not translate user-provided labels.

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
