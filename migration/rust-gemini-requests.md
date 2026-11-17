# Native Gemini text and voice requests

The request builder sends only the approved text schemas or the original transcript contract. It preserves the configured model and thinking level, keeps `store: false`, and packages audio as the pinned Interactions SDK does. The same bundled instruction and original application schema validate the transcript reply. OpenAI and Anthropic text contracts expose no voice request. [Gemini Interactions API](https://ai.google.dev/api/interactions-api)

Twelve complete wire requests, with and without a thinking level, matched the pinned Python SDK using a local HTTP mock. The Linux ARM64 fixture also prepared a full 20 MiB synthetic audio input and rejected the next byte before encoding. Peak memory was 58,454,016 bytes under 96 MiB with no swap. No provider endpoint was contacted.
