# Rust migration – HTTP scopes

The native capabilities endpoint accepts the existing legacy administrator
key and scoped token configuration. Process values override the local
settings file. The server compares fixed-size credential digests and
rejects invalid configuration without printing the submitted values.
Read and write scopes remain independent. Wearable configuration requires
a dedicated device and excludes other scopes.

`check_api_scopes.py` compared 52 HTTP responses and 4,096 tool-policy
decisions with pinned Python. Native Linux ARM64 passed the loopback
HTTP and CLI checks in a 32 MiB cgroup, with a peak of 14,606,336 bytes,
zero swap and no OOM events. The checks cover redacted startup errors,
settings priority, authorization and method handling, cache behavior,
and the known and unassigned tool names for every scope combination.

Native tool execution and data-driven consent checks are separate work.
