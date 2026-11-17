# Shared budget for interpretation and analysis

Rust counts interpretation and answer requests against one six-call budget. Every request counts its UTF-8 instruction and prompt bytes plus the approved application schema's bytes as measured by the Python baseline. The limits are 96,000 bytes per request, 384,000 bytes for the message, and 120 seconds of elapsed monotonic time. A rejected request does not consume the remaining budget.

The same budget reserves the final answer turn, caps read attempts at 12, and checks the next prompt's serialized evidence against its 48,000-byte limit. The caller can pass the remaining time to the provider transport and recheck it before reading evidence.

The two ARM64 tests passed in a 32 MiB container that peaked at 7,233,536 bytes with no swap. The paired recorder matched all 29 Python decisions, including Cyrillic UTF-8 boundaries, a combined six-call limit, aggregate bytes and the exact timeout. The second test checks the final answer turn and the independent read and evidence limits.
