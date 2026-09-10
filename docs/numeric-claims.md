# Numeric evidence validation (GA-23, second phase)

Analytical final answers may contain bounded numeric_claims with an evidence ID,
a typed key/index path inside that tool result, and an expected finite number.
The application resolves the exact path in successful evidence from the current
analysis, requires that evidence ID to be cited, compares the value, and renders
the source value with its tool and JSON-pointer field provenance. A claimed mean
of 87 fails when mean is 78 even if max is 87 in the same result. Null, strings,
booleans, missing paths and failed tools cannot establish numeric evidence.

Free-form numeric characters (including Unicode numeric characters) in the
qualitative answer are rejected; numbers must use the structured route. The model
is instructed not to spell numbers out to evade this contract. Urgent safety uses
the existing fixed emergency response. Rejected answers do not enter conversation
memory. Qualitative answers without numbers remain supported.

This phase proves exact numeric citations, not complete semantic correctness of
natural-language prose. Spelled-out quantities, causal claims and relevance to the
question are not deterministically verified. The technical provenance is explicit;
friendly metric-specific templates, date/unit rendering, reproducible persisted
AnalysisRun records and complete claim-coverage validation remain later phases.
No live model or real health data was used for verification.
