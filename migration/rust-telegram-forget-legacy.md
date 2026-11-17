# Forget Telegram analysis across the transition

The native memory store can clear the authenticated Telegram conversation and the legacy owner's analysis cache in one transaction. It rotates the current memory epoch, removes shared turns that depended on the cleared conversation, and redacts legacy analysis replies. Older pending turns are removed. Independent conversation state and diary replies remain available.

The transaction uses the same policy and legacy context locks as Python. An already active legacy send causes a short busy response before either memory store changes.

The disposable database test matched the complete legacy state produced by pinned Python, including four redacted replies and the preserved diary reply. It also verified removal of the dependent neutral turn, a non-Telegram rejection, and atomic behavior while the legacy context lock was held. Linux ARM64 used 7,757,824 bytes in a 32 MiB container with no swap.
