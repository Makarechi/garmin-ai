# Native Telegram daily summary

The short control path handles `/today` using the latest saved day while Garmin data is ready. It captures the replay generation and sends the recorded reply through the existing confirmed Telegram outbox. If replay starts or the generation changes, delivery substitutes the replay notice under the same storage guard and records the text actually sent. A daily summary is bound to the original command revision.

The native Linux ARM64 control test in disposable TimescaleDB exercised an empty store, the latest of two days, missing metrics, signed zero and scientific notation, replay already pending, and a projection change between claiming and delivering the reply. It checked both the delivery callback and the resulting stored reply. The original pause, resume, forget, retry and pairing tests passed in the same run.
