# Caffeine timing and main sleep (GA-19, first phase)

analysis_coffee_sleep performs a bounded descriptive comparison in code. A fixed,
returned specification defines main sleep as the observation unit, a pre-sleep
24-hour diary window, a configurable late timing threshold, an outcome of sleep
score or duration, and exclusions. The range is at most 31 calendar dates.

A new explicit caffeine_log_complete interval records the user's assertion that
all caffeine in a stated interval is logged. It is never inferred from silence or
one drink. Confirmed completeness/absence intervals must cover the entire window;
contradictory absence, missing sleep/outcome and recorded illness/travel exclude a
night. Partial dose ranges remain ranges, unknown dose remains unknown. Zero
requires explicit complete coverage and no intake. Notes and original text are
not copied into analytical evidence.

Both timing cohorts need five nights for an exploratory mean comparison. Existing
blocked resampling requires fourteen observations per cohort; otherwise uncertainty
is explicitly unavailable. These are descriptive evidence thresholds, not proof of
causality. The result includes all eligibility reasons, source references, event
revisions, method version and a deterministic hash of the specification and inputs.

A persisted feature store and AnalysisRun, holdout validation, time-at-risk migraine
models, multiplicity control and sensitivity analyses remain later phases. Absence
of a confounder record does not establish absence of the confounder. No automatic
questions, treatment advice or changes to caffeine intake are produced.
