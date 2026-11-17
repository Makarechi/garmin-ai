# Native Telegram polling cursor

Rust reads and updates the existing per-bot polling offset and owner ordering epoch. A saved offset expires after seven days without ingress. Older offsets can use the saved ordering time, and older primary databases can derive their first ordering epoch from the last retained Telegram update. Updates from other chats advance the bot's polling offset without changing the owner's ordering epoch.

Nineteen cursor and ordering snapshots matched pinned Python and its local fake bot. Native checks covered the exclusive ingress lock, an invalid stored offset, and rollback of cursor and epoch changes with the enclosing transaction. Linux ARM64 used 7,753,728 bytes in a 32 MiB container without swap. The polling runner supplies the verified update and writes the cursor and accepted event in the same transaction. [Telegram Bot API polling contract](https://core.telegram.org/bots/api#getupdates)
