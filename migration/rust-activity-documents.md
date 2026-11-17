# Legacy Garmin activity documents

The activity JSON path applies details, splits, typed splits, zones and weather
to the existing ActivityPart records. It checks the owner and training
collection permissions again before applying captured input. Data from a
different activity or endpoint cannot be applied under the requested scope.

Rows keep their array order and unknown fields. A repeated response keeps its
original interpretation timezone. A newer correction can bring an older digest
back into use. A late response leaves the most recent attempt in control.
Empty or failed attempts keep the last applied details and record the newer
attempt separately. A savepoint rolls back partial row replacement before
recording an application failure. The original archive stays available to retry.

The native ARM64 test uses a disposable TimescaleDB. It covers owner and endpoint
mismatch, correction and duplicates, timezone retention, delayed and empty input,
and a forced constraint failure after replacement has begun. It verifies that
the successful pointer and rows survive, then removes the synthetic constraint
and retries the same archived input. The retry clears the parser-failure marker.

Activity summaries, full history reconciliation and device-level samples are
covered by separate ingestion steps.
