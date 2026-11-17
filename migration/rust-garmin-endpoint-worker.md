# Native Garmin endpoint worker

The endpoint worker uses the authenticated legacy reader for a saved date,
device, or activity endpoint. It checks consent and the persisted integration
pause before a fetch, renews the job during network and archive work, and
publishes captured data under the same current lease. Rate limiting pauses
work already claimed elsewhere. The response body stays separate from the
integration and error records.

A Linux ARM64 run connected the worker to disposable TimescaleDB and a
synthetic transport. A 1.3-second response succeeded with a one-second
lease. A changed lease stopped a pending fetch, and consent revoked during
a different fetch prevented capture. Rate limiting preserved previous
observations, persisted at least the established cooldown, and blocked the
second already leased job without requesting another response. The worker
fixture peaked at 7,852,032 bytes in a 32 MiB container with no swap.
Fifteen existing client tests also passed on Linux with local fake servers.

The service scheduler still needs to activate and supervise this worker
alongside activity pages and FIT downloads.
