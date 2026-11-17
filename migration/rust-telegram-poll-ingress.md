# R27 Telegram polling ingress

The native worker can accept a verified polling update while holding the same exclusive PostgreSQL session lock as the Python runtime. For each update it checks the saved primary and fallback selection. It commits the neutral inbox message, button consumption, ordering state and polling offset together. Edits preserve their original operation and leave the message ordering timestamp alone. Foreign, deselected and unsupported updates only advance the corresponding bot offset. A database error leaves that offset available for replay.

The integration test uses the existing pinned Python button recorder on a disposable TimescaleDB. It checks repeated messages and buttons, edit lineage, primary and fallback selection, an exclusive policy change, unproven callback times and a forced failure on the final offset write. That write rolls back the inbox entry and restores the one-use button. The retry then commits them together.

Outbound acknowledgements, webhook intake and legacy callback dispatch still need runtime integration.
