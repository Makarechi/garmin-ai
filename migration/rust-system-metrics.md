# Native system metric definitions

The native registry installs the 11 built-in metric contracts from the pinned
Python implementation. It uses the same advisory lock, namespace and contract
hash. A fresh database gets all definitions. An existing database reuses the
same identifiers when the contract matches and appends an immutable version
when it changes. Registration leaves measurements and historical versions in
place.

The Linux ARM64 test uses isolated TimescaleDB databases with and without a
registry installed by pinned Python. It compares every field and digest for all
11 metrics, checks identifier reuse in both directions, and introduces an older
immutable version to verify that upgrading the current version preserves it.
No operational schema was changed.
