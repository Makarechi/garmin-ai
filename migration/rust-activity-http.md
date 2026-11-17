# Native activity tools over HTTP

Authorized health clients can discover and call the activity summaries
and activity details tools through the Rust HTTP service. Activity details
still exclude sample families by default. Explicit sample requests use the
same pagination and continue with the returned offset. The read holds one
verified snapshot through the request so archival replay cannot expose a
partially restored activity.

A synthetic TimescaleDB comparison matched 78 HTTP responses from the
pinned Python service, including the existing health tools. It checked
discovery schemas, credential scopes, timestamp coercion, status codes,
missing activities, pagination, and replay blocking. The activity store's
25 existing page comparisons also passed. Tests used local HTTP and fixtures.
