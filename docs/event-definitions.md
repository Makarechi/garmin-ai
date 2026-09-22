# Versioned event definitions (UNI-03)

The event registry separates the meaning of a record from the record itself. Existing
diary types are registered under `system.*` and continue to use their trusted Pydantic
validation. Custom types use the `user.*` namespace and cannot replace system IDs.

A custom definition starts as a draft. Activation requires the separate
`manage:definitions` API scope and creates an immutable version. Later semantic changes
are proposed with the current revision and activated as a new version; existing facts
keep their original version. Definitions can be retired without deleting their history.

The supported schema profile is a closed JSON Schema Draft 2020-12 object with at most
32 fields. It supports scalar types, bounded arrays, enums, numeric/string bounds,
bounded `oneOf`/`anyOf`, and local `$defs` references. Remote references, regex patterns,
unknown keywords, executable code and unbounded schema/data depth are rejected. Field
metadata carries stable IDs, labels, semantics and fixed units outside the data schema.

For example, `user.focus_session` can define an open episode with ordinal `focus`
from 1 through 5 and nonnegative integer `distractions`. Once activated, `/entries`,
the write-enabled MCP `entries_create` operation and direct domain calls all use the
same validator. Extra fields, out-of-range values and mismatched units fail before an
event or audit row is written.

Events store actual topology: a point has no duration, an unfinished episode overlaps
later query windows, and a closed episode becomes a bounded interval. Queries use the
stored topology rather than a hard-coded list of built-in event names.
