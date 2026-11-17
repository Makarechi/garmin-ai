# Validated analysis steps

A model step can request reads, return a verified final answer or signal urgency. Urgency ends the step before reads. The answer-only round refuses additional requests for tools. A final response requires at least one selected, successful result. Numbers in the free-text answer and values that differ from their cited evidence are rejected before rendering.

The pinned Python loop and native step matched eleven decisions from synthetic provider replies. They cover a verified narrative, a numeric-only answer, absent or failed evidence, invented values, numeric Unicode in prose, urgency and empty responses. A separate test covers the final tool-free turn. Both passed on Linux ARM64, peaking at 9,551,872 bytes in a 32 MiB container without swap.
