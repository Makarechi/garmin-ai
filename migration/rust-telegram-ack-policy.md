# R27 callback acknowledgement authorization

The native callback job records the confirmed private chat from ingress. At dispatch, the worker checks that the lease, owner, confirmed chat and selected channel are current. Pairing and selection locks remain held through the request. A mismatched or unproven chat closes the job without a network call.

The Linux ARM64 PostgreSQL fixture and a loopback Telegram stand-in proved that callback acknowledgement remains independent of the long poll, a provider rate limit still schedules a retry, concurrent pairing and channel changes are excluded during the request, and the retry is retired after the pairing changes. The fixture uses synthetic records and a loopback endpoint.
