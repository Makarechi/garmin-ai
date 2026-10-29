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
