# GA-31: restricted wearable upload contract

`POST /wearable/marks` accepts batches of 1–20 caffeine or medication reports.
This is the server contract for a future Connect IQ client, not a released watch app.
Each mark has a stable UUID, aware `device_time`, IANA `timezone`, optional device-reported
`clock_uncertainty_seconds`, and the existing explicit caffeine/medication payload.
Medication name, quantity and unit must be reported; the server never supplies a dose.
No references to other diary records, edits, deletions or archive reads are allowed.

Configure a separate `GA_API_TOKENS` entry with a random key of at least 32 characters,
`scopes: ["write:wearable"]` and `wearable_device_id` (UUID). This scope cannot be combined
with other scopes. Keep the same device UUID when rotating its key; remove its token and
restart the API to revoke it. Existing owner/API keys do not authenticate this endpoint.
Never provision a Garmin, Gemini or administrative credential on the watch.

The response contains only `{acknowledgements: [{id, accepted: true}]}` after the transaction
commits. A future client must retain each immutable local mark until that ACK, retry the
same UUID/content after lost responses, and never reuse IDs. Identical uploads receive
identical ACKs, including after owner correction/deletion and key rotation. Changed reuse
returns 409 and rolls back the entire batch. Device identities isolate UUID namespaces.

Every report is initially `needs_confirmation` with confidence zero and source `wearable`.
The supplied timestamp is preserved as unverified device time, not replaced by receipt time.
A durable receipt stores server receipt time and declared uncertainty separately. Even a
claimed zero uncertainty does not establish clock accuracy. The owner can review/correct
and confirm through existing diary editing; this phase does not silently record a confirmed
intake or assume that a delayed report has an accurate clock. Acknowledgement means durable
receipt, not owner confirmation or a recommendation to take medication.

Receipts are included in full backup/erasure and retained with idempotency history; deleting
an event does not remove its receipt or resurrect it on replay. No receipt retention job is
introduced. Watch-local storage, queue overflow handling, pairing UI, automatic clock-offset
measurement, summaries, migraine controls and device/simulator QA remain later phases.
Tests use synthetic data and HTTP clients; no physical device or external provider is used.
