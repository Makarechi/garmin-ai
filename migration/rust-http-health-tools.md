# Rust migration – health tool HTTP routes

The native executable now exposes the existing health snapshot and range
through scoped tool discovery and execution. It verifies access before
reading input, and acquires the shared replay fence in the same transaction
as the health query. An archive rebuild returns the established maintenance
response with its retry interval. Discovery publishes the two implemented
health tools and their pinned schemas.

On native Linux ARM64, 34 requests matched the pinned Python service over
a disposable TimescaleDB schema. The checks covered scoped discovery and
execution, invalid credentials, normal and missing summaries, numeric and
offset date inputs, invalid arguments and replay readiness. The fixture
compares discovery for the implemented tools. The candidate limits tool
bodies to 1 MiB and redacts malformed JSON into the common invalid-argument
response. Remaining HTTP tools and detailed decoder errors stay open.
