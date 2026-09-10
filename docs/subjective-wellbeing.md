# Subjective wellbeing (GA-22, first phase)

`wellbeing_observation` is a point-in-time diary report. It records optional integer
0–10 energy, restedness, pain and functional impact, plus the owner's notes. Higher
energy/restedness means more of that reported outcome; higher pain/impact means more
pain/interference with ordinary life. These are explicit user ratings, not validated
clinical scales. Free text does not imply a numeric value. At least one report is required.

Existing event API/MCP writes provide revision checking, auditing, idempotency and undo.
The `wellbeing_observations` read tool needs only diary permission and returns up to 200
reports in a half-open range of at most 31 days. Large content asks for a narrower range.
It preserves event source, status, revision and timezone. Missing answers remain unknown.

Garmin values remain separate evidence. High Body Battery never overwrites a report of
feeling exhausted. The analytical prompt requests subjective evidence for wellbeing
questions and asks for outcome-specific wording; model adherence is not claimed proven
by the deterministic tests.

Tests use synthetic reports and real disposable PostgreSQL, including disagreement with
vendor scores, zero versus missing ratings, range boundaries and corrected/deleted reports.
Goal selection, opt-in check-in scheduling, activity RPE and outcome-specific insight
generation remain subsequent GA-22 phases. No live model or clinical validation is claimed.

Tool evidence omits duplicate original_text and idempotency metadata; notes longer than 2,000 characters are shortened with notes_truncated=true. The complete diary entry remains stored. Correction commands follow the existing full candidate EventInput plus changed_fields contract: the interpreter copies unchanged fields before clearing one rating, and only explicitly changed fields are merged. An empty final report remains invalid.
