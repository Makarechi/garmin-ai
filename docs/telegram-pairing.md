# GA-25: first-owner Telegram pairing

After normal setup, store the bot token in the instance .env and leave
GA_TELEGRAM_USER_ID absent or zero. Stop the worker, then run
`uv run garmin-ai pair-telegram --env-file .env` on the host using the instance's
configured database access. The local console prints a fresh three-minute code
and the bot username. Send that command in a private chat with that bot.

Only a new, non-forwarded private message whose sender owns the chat can match.
The local file is atomically updated with the owner ID; other settings remain.
Existing owner bindings and webhooks are refused. A changed .env aborts pairing.
A timeout or interruption leaves the owner unset. Restart the instance afterward.
The code authorizes the first owner: share it only with the intended owner.

This avoids manual chat-ID lookup but is not a hosted onboarding wizard. It does
not enroll or change Garmin account identity. Tests use synthetic tokens and fake
Telegram objects; no real pairing was performed by this change.
