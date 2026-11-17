# Durable Garmin daily history windows

The native scheduler creates at most two new source days per pass. It catches
up after an outage before requesting older history, filters endpoints using
the selected collection settings, and retries up to two previously disabled
windows. The owner, timezone, history horizon and permitted endpoints
determine the saved cursor. Existing windows are reused after restart or a
preference change.

A bounded query finds missing dates. Newly enqueued jobs, their windows and
the continuation cursor commit together. The scheduler writes the same
legacy job payloads and does not call Garmin.

`check_history_windows.py` compared pinned Python and native Linux ARM64 on
two disposable TimescaleDB databases. Eleven scheduling passes created 210
matching jobs and left matching plan and window records. The fixture covers a
DST transition, restarts, outage recovery, collection changes, a wider
horizon, disabled-window retry, and owner and mutation locks.
