# Native validation of custom diary schemas

The Rust validator applies the existing bounded Draft 2020-12 profile used for
custom diary contracts. It accepts closed objects with stable names and
bounded field types. It checks numeric and array limits, contradictory
branches, numeric literals and Unicode length. It only expands local references
within the existing depth and complexity limits.

A differential test compares the pinned Python and Rust verdicts on 207
synthetic contracts. It exercises bounded and contradictory numeric ranges,
recursive and missing references, closing nested objects, equal alternatives,
literal mismatch and the encoded size limit. Definition management and custom
entry storage are separate changes.
