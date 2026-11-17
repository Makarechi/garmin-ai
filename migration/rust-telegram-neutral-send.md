# R27 Telegram neutral message transport

The native transport sends literal neutral replies in order, with the inline keyboard on the first part. It verifies the returned message identifier and private chat. Provider acceptance is distinct from confirmed delivery. A rate limit before the first accepted part exposes the retry delay. Later failures retain the last accepted identifier and return an uncertain result. A lost response also returns an uncertain result. The transport stops at the first failed part.

Three requests recorded from the pinned Python Telegram SDK matched. Local tests exercised first-part rejection and rate limit, partial acceptance, connection loss, a wrong response chat and a mismatched bot binding. All six transport tests passed offline on Linux arm64. Peak memory was 11,878,400 bytes with zero swap in a 32 MiB container.

The outbox worker must store these transport outcomes under its delivery lease.

[Telegram sendMessage contract](https://core.telegram.org/bots/api#sendmessage)
