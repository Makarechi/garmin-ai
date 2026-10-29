# Model response schema projection

The OpenAI schema compiler projects typed application response models onto the
[Structured Outputs subset](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas).
It closes objects, marks every defined property required and translates unions
and constants. Fields with application defaults must still be present in a model
response. Open dictionaries, untyped values and unsupported schema composition
are rejected instead of silently omitting arbitrary fields.

Titles, descriptions and defaults are omitted from the transport schema. This
does **not** make arbitrary generated schemas safe to transmit: property names,
enumerated values, constants and patterns remain part of the schema. An adapter
must restrict schemas to trusted, application-owned definitions or apply an
explicit policy before sending them. The application must validate every response
with the original model; the wire schema does not replace domain validation.

Compilation tests use synthetic values and do not establish model compatibility
or live provider behavior.

The Anthropic projection uses the same closed-object structure while omitting
numeric constraints, patterns and array limits from the transport schema, and
rejecting recursive references. The original application model remains the
authority for those checks. Anthropic documents that schema grammar is cached
separately from message content; do not place personal information in property
names, enum or constant values, or patterns. Restrict transmission to trusted
application-owned schemas. See the [Anthropic structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
