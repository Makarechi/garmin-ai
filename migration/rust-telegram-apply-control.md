# Durable Telegram owner controls

The short control worker can apply pause, resume and conversation forget under the current confirmed Telegram binding. The command, reply and completed inbox job commit together. The handler reads Telegram's stored sequence and ingress timestamps. It uses the existing web and Telegram pause settings, and the same conversation forget behavior as the legacy path.

A disposable TimescaleDB run verified the three exact confirmation messages, independent progress during a leased diary message, rejection of a changed binding, duplicate delivery, and rollback if writing the reply fails. Retrying the same lease after the simulated storage failure applied the command once. The Linux ARM64 test peaked at 7,659,520 bytes under 32 MiB, without swap or OOM events.
