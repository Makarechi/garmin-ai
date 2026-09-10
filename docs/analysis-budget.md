# Analysis request budgets (GA-23, first phase)

A normal Telegram analytical request shares one budget from intent classification through
the final answer: at most six provider rounds and twelve read-tool calls.
Classification input and elapsed time are charged to the same budget.
The last available round requests an answer only. A model that requests additional
tools cannot bypass either bound.

Before each provider call, the request is checked against a 96,000 UTF-8 byte input
limit and a 384,000 byte cumulative input limit. These include the instruction,
serialized result schema, tool descriptions, question, quality context and repeated
evidence. Evidence across all tool results is separately capped at 48,000 UTF-8 bytes.
These are deterministic byte budgets, not claimed exact provider token/cost accounting.

The existing per-result limit reports `result_too_large`; it is never valid evidence
of absence. Exceeding a shared budget returns a distinct notice asking for a smaller
period or one metric. No partial evidence is silently discarded to produce an answer.

A 120-second elapsed deadline prevents starting further model/tool work. It cannot
cancel a synchronous request already running; the provider's own timeout still applies.
Voice transcription and the separate safety screen for oversized/reordered messages precede
this analytical budget and retain their own limits. The daily token/cost budget, structured numeric claims, durable analysis provenance and
an evidence inspection button are subsequent GA-23 phases.

Acceptance checks use synthetic tool results, real disposable PostgreSQL, fake elapsed
time, multibyte text, repeated requests and an intentionally oversized tool response.
No new live provider call is required by this phase's tests.
