# Native layered timeline

The owner view reads activity, interval and diary records from one
replay-verified snapshot. It keeps separate layers for sleep, activity,
wellbeing, context and plans. Points remain annotations. Open episodes retain
their missing end without claiming duration. The legacy segment label and
overlap order still use the original priority, confidence and tie rules.

Twelve cases matched the pinned Python output and errors, including
timezone offsets and the 500-annotation limit. Two overflow cases exercised
the native caps of 4 MiB for stored interval evidence and 16 MiB for the
rendered document. A pending archive replay made the read unavailable.
The native Linux ARM64 fixture peaked at 24,674,304 bytes in a 32 MiB
container with no swap.
