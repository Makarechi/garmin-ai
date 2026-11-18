# Native context question generation

R30, `proactive_questions_and_tracker_initiatives` in `parity.yaml`. Reference
commit `ee3a14a48a6ad30c1085951bac2f53c9088c2450`.

The generator uses stored observed Garmin heart-rate and stress samples.
It checks the personal baseline, stress duration, sample coverage, and
known activity and diary intervals. Confirmed calendar plans cover no
time. The saved owner timezone determines the local dates in the question.
An incomplete replay leaves the same half-hour window eligible for
generation later. An overlapping active or previously delivered question
suppresses another prompt. The generator reuses a cancelled unsent
question only when the owner did not cancel it through pause.

`check_context_question_generation.py` captured the Python questions on
disposable TimescaleDB. The Rust integration test compared the entire
stored question after initial generation, overlap suppression, and
reuse. The fixture includes local midnight, partial and complete diary
coverage, an external calendar plan, and stress samples continuing past
the scan boundary. It also checks the pack restriction, replay
deferral, and preservation of an owner-pause cancellation.

The native Linux ARM64 context run peaked at 8,896,512 bytes in a 32 MiB
container. The existing migraine and caffeine generation run also passed
and peaked at 6,262,784 bytes. Neither run used swap.

Context delivery, collection scheduling, and live validation remain open.
