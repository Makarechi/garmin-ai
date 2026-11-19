# R25 persisted tracker safety recovery

The frozen reference `ee3a14a48a6ad30c1085951bac2f53c9088c2450` stores `checked`, `urgent` or `unavailable` in the Telegram job payload. The next attempt reuses that status. A current tracker/model disclosure denial bypasses the old result.

## Acceptance for this change

- Reuse each valid saved status after an expired claim.
- Preserve all other job payload fields when recording the result.
- Reject writes from expired claims and check the current selected version.
- Recheck current model disclosure permission before using a saved result.
- Persist a fresh positive, urgent or failed safety response before tracker extraction.
- Preserve neighboring tracker facts, original text, evidence, contexts and receipts.

## Evidence

The source helper captures 24 initial-state/outcome combinations over 72 repeated calls. A second fresh capture produces identical fixture documents. The fixture includes null, boolean, numeric and unknown string states alongside the three valid statuses.

Each native English and Russian model conversation also runs the 24 metadata contracts, three expired-claim recovery scenarios, a disclosure revocation after the status was saved, and three fresh synthetic SDK outcomes. The recovery scenarios preserve the saved classification and use one extraction call for checked/unavailable text. An urgent cached classification completes locally. A withdrawn model grant produces a local form without a model call.

The normal native worker is used for these scenarios. Failed outbox publication still rolls back the event, audit, context and receipt, and the subsequent attempt commits one entry and reply. The previous 12 model dialogue steps per locale retain full source comparisons.

The model proof runs inside 48 MiB containers with swap disabled. Wider voice, transport, malformed legacy state, combined channel/release acceptance and the final seven-day soak remain pending.
