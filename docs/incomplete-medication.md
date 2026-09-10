# GA-13: incomplete reported medication intake

Medication events record an explicitly reported intake even when name, dose or unit is unknown. Unknown fields are null, never inferred from a prior prescription, recent migraine or typical dose. Existing complete payloads remain valid; no schema migration or rewriting of history is needed.

The offline medication form accepts `неизвестно; неизвестно; сейчас` or a known name with `неизвестно` dose. Known doses still require explicit supported units in the form. The response and diary display name/dose/unit gaps. This records a reported intake, not a prescription or a suggestion to take a medication. Questions and denials must not create intakes.

API/MCP and ordinary correction commands can fill in missing fields through the existing revision and audit protocol. Undo restores the incomplete record. Original user text stays attached. Dose totals or medication-specific conclusions cannot be inferred from missing fields. Effect observations, negative intake intervals, medication presets and uncertain time ranges remain later GA-13 phases.

Validation uses synthetic explicit forms with/without a failing provider, duplicate delivery, domain correction and undo; no clinical validation or live model behavior is claimed.
