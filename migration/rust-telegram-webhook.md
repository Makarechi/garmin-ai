# R27 Telegram webhook ingress

The configured webhook verifies one Telegram secret header before reading the body. It bounds the body to 1 MiB and rejects malformed JSON with HTTP 400. It honors the saved channel selection, serializes ingress with the existing PostgreSQL lock and handles repeating updates without creating another inbox event. The callback acknowledgement job commits with the event. Webhook reception does not establish the time a button was pressed.

Ten HTTP response documents match pinned Python, including bad secrets, oversize input, private-chat scope, a repeated request, an edit, a busy database and a successful retry. Native checks also cover a repeated secret header and malformed input. The paired route was exercised over loopback with the pinned schema. Peak memory on Linux arm64 was 10,907,648 bytes under a 32 MiB cap without swap.

The receiver is available as an explicit route constructor. Deployment selection and webhook registration are separate steps.

[Telegram webhook delivery contract](https://core.telegram.org/bots/api#setwebhook)
