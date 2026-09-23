# Personal onboarding

The onboarding plan follows the user rather than a bundled author profile: language, timezone and
units; selected scenario packs and custom trackers; optional sources; channel; explicit model data
categories; reminders; then a capability status page. No source or channel is mandatory.

Plans are idempotent. Re-running setup updates only selected preferences, enables or disables the
chosen packs, and creates only missing trackers. It does not delete events, rotate keys, rewrite
consent, import secrets, or require Garmin authorization. A later source connection likewise does
not replace the local diary.

English and Russian messages use stable resource keys. Changing language affects presentation, not
definition keys, field IDs, event payloads, units, metric scales, or historical versions. Advanced
tracker manifests contain only strictly validated tracker data; integration settings, credentials,
callbacks, and code are rejected.
