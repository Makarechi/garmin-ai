# Native tracker sharing checks

A sharing check uses the recorded definition version and the exact model or
channel destination. Sensitive schemas and facts require the selected
categories, and original text always requires explicit consent. A consent
from the future or a stored consent with inconsistent identity cannot
authorize delivery.

The delivery guard takes the same PostgreSQL locks as the pinned Python
application. It rechecks every required version before starting a callback.
Channel delivery also holds the shared replay lock, in the order used by
legacy revocation. Cancellation drops the read transaction and releases
its locks. A provider callback that returns has no later commit step that
could incorrectly turn its completion into a new send attempt.

Thirty decisions matched pinned Python on disposable TimescaleDB. The native
ARM64 fixture also confirmed that an exclusive consent write blocked
delivery, the guard prevented concurrent revocation, cancellation released
the lock, and revoked or mismatched consent denied the next callback.
Application-specific revocation and pagination wiring remain separate work.
