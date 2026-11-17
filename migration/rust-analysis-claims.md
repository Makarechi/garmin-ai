# Verified analysis citations

The native agent can render numeric and timestamp claims from successful, selected read-tool evidence. Each claim must use the exact value at an existing object field or array index. Evidence IDs distinguish separate reads of the same tool, and output paths are escaped consistently. Integer comparisons preserve large values exactly. Floating-point values must be finite.

Timestamp claims match the original string and require an aware ISO timestamp. Rust renders the local clock and UTC offset from the owner's timezone. The sleep labels come from the existing language catalogs.

The offline comparison matched all 36 pinned Python cases, including invented values, a string in place of an array index, error results, very large integers, two identical local clocks on different sides of the DST boundary, and localized sleep timestamps. A separate test rejects infinite numeric evidence. The Linux ARM64 run peaked at 7,495,680 bytes in a 32 MiB container with no swap.
