# Native tracker sharing consent updates

Integration managers can grant, narrow and revoke tracker sharing consent.
The mutation shares the existing consent and delivery locks with Python.
Narrowing a channel grant cancels its queued dependent replies and clears
that channel's saved analysis. Narrowing model facts or original text
clears derived model context. The consent change and cleanup are atomic.

Fourteen synthetic writes matched pinned Python, including authorization,
absent consent, future timestamps and separate channel instances.
Held delivery locks prevented cleanup from racing an in-progress send.
A forced cleanup error restored the prior consent and queued messages,
and a retry succeeded. The native Linux ARM64 fixture peaked at 8,323,072
bytes in a 32 MiB container with no swap. The existing 30 consent read
decisions and delivery lock tests also passed.
