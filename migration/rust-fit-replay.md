# Native FIT replay from the archive

The native replay path opens the verified original FIT or ZIP and stages
records in bounded batches. Before publishing it rechecks the recorded
source and owner under the replay lock. FIT parts, the parser generation,
status and replay outcome commit together. Older installed exports retain
the later attempt watermark. The archive remains usable with training
collection disabled.

Eight synthetic TimescaleDB cases matched the pinned Python FIT
projections and replay outcomes. The comparison included two ZIP members,
a retained older projection, an empty export, a superseded export, an
already current parser, a digest mismatch and a late CRC failure. The
native test also checked cancellation, mismatched owner and a forced
publication failure followed by retry. The existing live FIT publication
test passed in a 32 MiB Linux ARM64 container with no swap.
