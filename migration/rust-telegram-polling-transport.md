# Native Telegram polling transport

The native client performs one HTTPS request per poll or callback acknowledgement. Polling uses the saved positive offset and the message, edit and callback update types. An acknowledgement has its own connection pool and deadline, so it can complete while a long poll waits. Error messages retain the retry delay and class while omitting the provider response and credential. Both responses have size limits. [Telegram Bot API polling](https://core.telegram.org/bots/api#getupdates) [Telegram Bot API callback acknowledgements](https://core.telegram.org/bots/api#answercallbackquery)

Three logical request bodies matched the pinned Python Telegram SDK. Four native tests used loopback only to verify request fields, concurrent acknowledgement, rate limits, response bounds, malformed input, redirects and authentication failures. Linux ARM64 used 7,757,824 bytes in a 32 MiB container without swap.
