# GA-25: first-owner Telegram pairing

During setup, before starting the worker, store the bot token in the instance .env and leave
GA_TELEGRAM_USER_ID absent or zero. Stop the worker, then run
`uv run garmin-ai pair-telegram --env-file .env` on the host using the instance's
configured database access. The local console prints a fresh three-minute code
and the bot username. Send that command in a private chat with that bot.

Only a non-forwarded private message containing the fresh random code, whose sender owns the chat, can match. Host/server clock skew does not invalidate the code; its lifetime uses a local monotonic clock. The matched Telegram update is acknowledged before the owner is saved.
The local file is atomically updated with the owner ID; other settings remain.
Existing owner bindings and webhooks are refused. A changed .env aborts pairing.
A timeout or interruption before saving leaves the owner unset. For Compose, use the exact recreation command printed by pairing from the instance project directory. With the default file it is `GA_WORKER_ENV_FILE="$PWD/.env" docker compose --env-file "$PWD/.env" up -d --force-recreate worker`. For another file, both paths must identify that file: the CLI flag configures Compose interpolation, while `GA_WORKER_ENV_FILE` configures the worker service environment. Keep both paths on future Compose invocations. `docker compose restart` or `start` retains the old container environment and does not apply the owner ID. For a host service, restart it with the updated environment.
The code authorizes the first owner: share it only with the intended owner.

This avoids manual chat-ID lookup but is not a hosted onboarding wizard. It does
not enroll or change Garmin account identity. Tests use synthetic tokens and fake
Telegram objects; no real pairing was performed by this change.

Dotenv variable references are resolved using the same interpolation as normal settings. The original file bytes remain the basis of the atomic owner-ID update; referenced secrets are not expanded into the saved file.
