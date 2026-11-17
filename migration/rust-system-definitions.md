# System diary definitions

The native registry uses the 19 system event contracts emitted by the pinned
Python implementation. It verifies each stored digest before offering the
contract to the database. Hashing preserves the existing compact sorted JSON
and ASCII escaping, including surrogate pairs, operation order normalization
and omitted optional metadata.

Registry installation uses the existing versioned tables and bootstrap lock.
A matching version keeps its identifier and revision. A changed contract
adds a new active version after the highest recorded version and leaves the
previous versions available to entries that refer to them.

The integration test passed against a newly migrated disposable TimescaleDB
and against one where the upstream Python implementation had already installed
all 19 definitions. It checked the stored schemas, labels, privacy settings,
field metadata and operation lists, including a synthetic previous contract
revision.

From the project root, regenerate the catalog with the locked Python environment.

```sh
uv run python migration/generate_system_event_contracts.py | cmp - crates/ga-db/contracts/system-events.json
```
