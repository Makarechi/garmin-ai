# R27 Telegram ingress selection and pairing

The native selection reads the existing integration list and Telegram owner setting. It uses the configured active Telegram instance, with the legacy primary namespace when the integration list is empty. It resolves the private chat against the database's sole owner and its existing confirmed pairing. An omitted integration is disabled and a missing or different pairing fails before ingress starts.

Ten selection cases matched pinned Python. The separate TimescaleDB test checked the legacy primary and explicit secondary binding, a missing or mismatched chat, and an in-progress ownership change. The Linux arm64 tests used 6,111,232 bytes peak with zero swap under a 32 MiB cap.
