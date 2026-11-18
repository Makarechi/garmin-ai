# Historical Telegram conversation repair

The native migrator accepts Alembic revision `a42d9e18c701`. It restores Telegram conversation IDs using the same identity function as live ingress. It moves the inbound and outbound history together, preserves the canonical row when it already exists for that instance, and rolls back a conflicting identity.

`check_telegram_schema.py` compared the pinned Alembic migration and native Linux ARM64 against 133 conversations on disposable PostgreSQL 17 with TimescaleDB. The fixture crossed the cursor batch boundary. It covered current IDs, an existing canonical target, another channel, and an absent external chat. It also injected a collision after earlier rows had moved, compared the unchanged database after failure, removed the conflict, and verified a successful retry.
