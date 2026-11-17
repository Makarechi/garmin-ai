# Durable replay scheduling

This is the bounded native scheduler for R16. It uses the existing queue and
archive-selection rules. The authenticated Garmin owner is checked before
queue work. The scheduler writes only jobs and its scan cursors.

The scheduler shares the legacy advisory lock and admits up to 25 jobs per
transaction and 100 pending or running jobs per parser version. It repairs
obsolete terminal jobs before admitting new work. Cursor updates share the
job transaction. An interrupted transaction leaves all progress unchanged.

`check_replay_queue.py` seeds two fresh isolated stores with the same synthetic
archive references and obsolete jobs. It compares five scheduling passes from
the pinned Python baseline and from a native Linux ARM64 Rust test binary,
including both repairs, all 100 queued jobs, their payloads, and both saved
scan cursors. Its database setup uses the reference runner's disposable-store
checks. Archive reading, publication and recovery are covered in separate
stages.
