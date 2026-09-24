# HF-02: initiative policy

The owner pause now applies to legacy questions, accepted insights, and channel-neutral tracker check-ins. A pause cancels queued check-ins, unsent questions, accepted insights, and insight reservations in the same transaction as the owner control. Resume leaves those retired items retired. Sync and explicit Telegram replies remain available.

The shared `notification_decision` evaluates owner control, pending inbound work, pending clarification, daily notification budget, quiet hours, and snooze. It returns allow, defer, or cancel with a reason and policy revision. Tracker check-ins additionally validate the current reminder enablement, local time, timezone, tracker revision, and sharing consent at queue and claim time. Tracker settings updates cancel queued projected check-ins immediately.

Delivery holds a session-level shared policy lock from final validation through the bounded network attempt. Pause and tracker settings updates take the exclusive lock, so a completed pause cannot race with an initiative send. A failed or ambiguous network attempt retains the existing uncertain-delivery behavior.

Synthetic database tests cover pause → resume without backlog, tracker disable/time/timezone/revision changes, and the send/change lock. Local Windows tests requiring runtime archive startup remain limited by the existing directory `fsync` permission issue; Linux CI is the full runtime gate.
