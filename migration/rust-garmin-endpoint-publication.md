# Leased Garmin endpoint publication

The database can check a queued Garmin endpoint before a fetch and publish a
captured response under the same job's live lease. The transaction records the
projection, history window, fetch freshness, and terminal or retry outcome.
The raw response remains available separately after a failed transaction.

Consent and account checks run before a fetch and again for publication. A
history job whose window now belongs to a different job completes without
fetching or replacing that window. Empty or unchanged responses keep the
current history source where appropriate. Older responses cannot overwrite
freshness, and future heart-rate or stress samples do not advance the
observation clock. Transport failures preserve earlier success clocks.

Synthetic PostgreSQL 17 with TimescaleDB 2.29.2 passed the native endpoint
transaction test and the existing activity-page publication test. A forced
failure at the history write rolled back the attempted projection, and a
retry with the same lease succeeded. The endpoint test also covered owner
mismatch, consent revoked before fetch, stale input and transport failure.
Peak container memory was 7,823,360 bytes with a 32 MiB limit and no swap.

The public worker still needs to call these primitives through a checked
transport and renew its lease while waiting for the response.
