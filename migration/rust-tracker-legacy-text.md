# R19 legacy selected tracker text

The frozen reference is `ee3a14a48a6ad30c1085951bac2f53c9088c2450`.
The comparisons use synthetic data in exact disposable databases migrated with the frozen Alembic catalog to `c54f8a72d130`.

## Acceptance for this change

- Existing selected tracker prompts resume through the normal Telegram workers.
- Offline text and captions open the existing generated form.
- Model text writes only values grounded in the message and preserves the first trusted clock, original text, source and receipt.
- Tracker and model consent apply before disclosure and again before publication.
- The event, audit, receipt, pending prompt, reply and completed inbox share a guarded transaction.
- Repeated ingress and expired claims preserve one committed entry.
- Replies to delivered analysis bypass selection, legacy and guided tracker prompts while retaining their state.

## Recorded comparisons

The capture helper runs the frozen `process_message` against real disposable PostgreSQL. The four stored conversations cover 66 offline and 24 model-backed steps in English and Russian. The native model conversations execute 22 synthetic SDK calls per locale. Model-denied sensitive tracker text reaches neither the safety screen nor extraction transport.

Both sides validate operation and context hashes using their own generated version IDs. Comparison normalizes generated IDs and form nonces, retains trusted message timestamps, and compares complete context and receipt documents. Snapshots sort equal-time entries by kind to avoid random UUID ordering.

The native failure proof raises an isolated database error while inserting the reply after the event and receipt have been prepared. The transaction rolls back the event, audit, context and receipt. A retry commits one entry, one reply and a completed inbox. The failed attempt retains the original selected prompt.

Separate native worker runs exercise analysis replies while selection, legacy and guided tracker prompts are pending. Each run executes seven synthetic SDK calls and verifies that the tracker prompt stays outside the analysis input.

All 363 frozen source tests in the Telegram agent, history, adapter and natural-language suites passed on a fresh isolated database. Local native defaults, strict all-target Clippy, paired HTTP extraction and source-ledger readback are recorded with the change validation.

## Resource scope

Offline conversations run with a 32 MiB container limit and model conversations with 48 MiB, both with swap disabled. These bounded synthetic proofs contribute to the final combined workload assessment under R37.

## Remaining work

Persisted safety-screen results have dedicated recovery evidence in `rust-tracker-safety-recovery.md`. Complete voice transcription, malformed legacy states, all channel paths, combined acceptance, final-build resource workloads and the actual seven-day soak remain open.
