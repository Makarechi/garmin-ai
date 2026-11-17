# Custom diary definition contracts

Rust validates the existing custom definition input, including localized
labels, stable field identities, allowed operations and metric semantics.
It uses the bounded schema profile and checks that declared fields match the
schema. Labels use the same language-tag casing and duplicate detection.
Derived duration and event-count definitions retain their existing constraints.

The pinned Python comparison covers 115 synthetic definitions. Accepted
definitions match in defaults, normalized labels, contract and stored hash.
It also checks rejected permissions, duplicate field identities and
contradictory schema combinations.
