# Time evidence for diary interpretations

The native interpretation keeps the timestamp offsets supplied by the model long enough to check them against the applicable timezone. A correction inherits the existing entry's timezone unless the change explicitly includes a timezone. Only changed timestamps are checked on a correction, and a close also checks its end time.

A timestamp that occurs twice during the return to standard time requires a matching offset in the exact owner message. Repeating the same hour and minute without the offset, or with a conflicting offset, asks for clarification. Unicode decimal digits are recognized in the clock, and the offset must match the explicit recorded offset. A point observation whose redundant end was removed during validation uses only its start. An offset that disagrees with the timezone and a time more than five minutes ahead of the reference clock are separate clarification outcomes.

The 23 synthetic local-time decisions recorded by pinned Python matched the native Rust validation in the ARM64 run. The 32 MiB container peaked at 10,227,712 bytes with no swap. The comparison includes both sides of the clock change, a skipped spring hour, identical times with conflicting evidence, saved correction timezones, and a one-microsecond difference at the five-minute boundary.
