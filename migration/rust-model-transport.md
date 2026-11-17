# Native structured model transport

The OpenAI and Anthropic transports send approved requests to the fixed
HTTPS endpoints used by the Python adapters. They retain Anthropic's
pinned API version, use the system trust store and prevent redirects,
proxies and automatic retries. Authentication headers are marked sensitive.
Upstream error bodies are discarded, and a bounded successful response
is checked using the exact schemas used to prepare the request.

The configured deadline applies to the whole HTTP request and body.
The client requests identity encoding to enforce the one MiB limit
during download. Cooldown reasons and retry times are returned for the
application to persist. The transport itself makes no fallback attempt.

Five Rust tests used local HTTP servers or pure configuration input.
They verified both provider bodies and headers, exact size and chunked
size limits, invalid configuration, redirected requests, interrupted
and delayed responses, refusal to cross provider boundaries, and typed
retry classifications. Linux ARM64 ran with external networking disabled
and peaked at 23,023,616 bytes in a 32 MiB container with no swap.
No live requests or customer data were used.

Consent and durable fallback remain at the application gateway boundary.
