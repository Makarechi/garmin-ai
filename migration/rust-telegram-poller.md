# R27 Telegram poll cycle and acknowledgements

Each cycle reads the durable cursor on its owned PostgreSQL runtime session and polls the selected bot. It commits owner messages and verified buttons in order. Any acknowledgement job is committed in the same transaction as its update and offset. The callback time is considered known only after a recent short batch. A full batch keeps notifications gated until the bot catches up.

The independent acknowledgement worker honors rate limits and records completed or expired callback answers. The synthetic Linux arm64 integration test uses the pinned schema, a 100-update backlog, two signed buttons, a week-old offset, a real loopback long poll and a rate-limited acknowledgement. It verified both time precisions and a separate acknowledgement while the poll waited. Peak memory was 12,443,648 bytes with zero swap under a 32 MiB cap.

The production runtime still needs to start and supervise these cycles under the configured Telegram mode.
