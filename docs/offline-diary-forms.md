# Diary forms without a model (GA-32, first phase)

When the provider is unavailable, the medication and note buttons open explicit text
forms. Medication uses `name; dose unit; time`, with a required numeric dose and one
of `mg`, `mcg`, `g`, `ml`, `tablet`, `drop`, `IU`. Note uses `text; time`. These record
owner-supplied facts; they never recommend medication or infer a dose from history.

Time may be `сейчас`, `HH:MM` in the last matching local day, or an ISO date/time with
an offset matching the configured timezone. Ambiguous/nonexistent DST wall times
require an explicit date and offset. Relative `сейчас` is anchored to the original
Telegram message timestamp. Invalid forms remain clarifications, not saved facts.
Existing inbox idempotency prevents duplicate records after retries.

The selected form expires under the existing clarification policy and can be
cancelled with `/cancel`. Free conversation, voice without a transcript, long-delayed
unparsed inbox reprocessing, confirmed medication presets and shared provider quota
cooldown remain later GA-32 phases. The form is not a medical triage system.

Synthetic tests cover model-free entries, explicit dose validation, retry deduplication,
original send time and DST boundaries. No real medication or provider calls are used.

Explicit semicolon forms are parsed before external interpretation even when a configured provider is unavailable. Free-form prose still follows the configured interpreter; invalid explicit forms ask for correction without a provider call.
