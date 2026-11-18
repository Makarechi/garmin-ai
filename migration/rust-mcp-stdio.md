# Native MCP stdio and diary reads

The native `mcp` command uses the Rust MCP SDK with a bounded stdio input. The handshake keeps the pinned server identity and instructions. The diary tool uses the existing model destination and rechecks owner, consent, scenario packs and tracker permissions inside a read snapshot. Original record text is excluded. In this stage the exposed tool is read-only.

The pinned Python MCP client compared initialization, the `events` descriptor, and twelve calls through both servers on disposable TimescaleDB. Results included scoped diary pages and denial after permission withdrawal. It also verified disabled writes, malformed argument handling, and no change to stored entries. Native process tests rejected an oversized request, serviced a following ping, and refused startup against an erased database.
