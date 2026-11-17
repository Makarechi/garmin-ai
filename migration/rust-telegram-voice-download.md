# R27 Telegram voice file downloads

The voice downloader resolves a Telegram file ID and fetches the result from the fixed Bot API file origin. It rejects a declared duration above ten minutes or size above 20 MiB before making a request. It checks the file metadata and enforces the byte cap while reading the download. Connection attempts, redirects, malformed paths and partial responses are handled without exposing provider details or tokens. The polling, acknowledgement and download clients have independent connection pools.

Two pinned Python HTTP calls matched the native voice requests. The local tests also exercised traversal attempts, partial content, a redirect, a rate limit and chunked audio at and beyond 20 MiB. All seven transport tests passed offline on Linux arm64. Peak memory was 32,833,536 bytes, within the 32 MiB cap, with zero swap and no OOM events.

Transcript storage and its tracker-consent check are follow-up integration work.

[Telegram getFile contract](https://core.telegram.org/bots/api#getfile)
