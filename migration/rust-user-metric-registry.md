# User metric contracts and history

The native validator keeps the established units, value kinds, aggregation
methods, coverage rules and ordinal scale constraints. It normalizes numeric
inputs before calculating the existing contract hash. Metric registration
requires definition management permission and an existing owner.

Registering an identical contract reuses its current version identifier.
Changing a contract adds an immutable version while the previous identifiers
remain available to their observations. Built-in registration uses the same
storage operation with the system namespace.

Pinned Python supplied 415 synthetic contract cases. The native ARM64 test
reused a Python-created metric and published an updated version. Pinned
Python then reused Rust's identifier and confirmed the original version.
The built-in catalog regression also passed against all 11 pinned records.

The decimal ranges used by ordinal score unit names are generated from
Unicode 17.0.0 as used by the pinned Python runtime. The differential
corpus checks both sides of every range boundary. This keeps validation
aligned with Python and adds no Rust dependency.

- [Python 3.15 Unicode database](https://docs.python.org/3.15/library/unicodedata.html)
