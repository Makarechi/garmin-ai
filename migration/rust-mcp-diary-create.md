# Native MCP diary creation

`events_create` is available when `GA_MCP_ENABLE_WRITES` is enabled. The handler uses the pinned input schema, binds the authenticated `mcp` producer, and passes the client cancellation state to the audited diary transaction. A stable idempotency key identifies the original write across runtimes.

The pinned Python MCP client compared the exact tool descriptor and replayed a Python write through Rust and a Rust write through Python. It checked that the same key with different values is rejected and that writes are disabled by default. A cancellation notification sent during the native database operation left the existing rows and audits unchanged. Retrying the cancelled key added one recorded entry and one audit.
