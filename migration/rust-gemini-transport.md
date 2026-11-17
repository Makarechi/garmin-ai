# Native Gemini Interactions transport

Gemini text and voice requests use the pinned HTTPS Interactions endpoint. The API key is a sensitive request header. The transport uses the system trust store, accepts identity encoding, and performs one attempt within the caller's deadline. It does not follow redirects or retry automatically. [Gemini Interactions API](https://ai.google.dev/api/interactions-api)

Successful responses are capped at 1 MiB and decoded with the original application schema. Error bodies are bounded at 32 KiB and only inspected for the invalid-key marker where it changes the outcome. Failure reasons and retry times remain typed, without including upstream response text.

Eight local tests passed on Linux ARM64 without external networking. They covered the existing text providers and Gemini text and voice, invalid credentials, quota, missing models, incomplete replies, oversized data and slow responses. The run peaked at 24,743,936 bytes under a 32 MiB cap with no swap.
