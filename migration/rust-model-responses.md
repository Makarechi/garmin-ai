# Native structured model response validation

The shared decoder checks the terminal envelope before interpreting the
model output. OpenAI reasoning and Anthropic thinking blocks carry no
result. A refusal, truncated response, unsupported tool block, or malformed
output cannot become an application value. A completed value must satisfy
both the provider contract and the local JSON Schema, including UUID and
timezone-aware timestamp formats. Optional usage
counters retain their strict integer bounds. Error messages contain no
response or prompt text.

Sixty-six synthetic response bodies matched the pinned OpenAI and
Anthropic adapters using local mock transports. The matrix covers missing
or malformed output, refusal precedence, token limits, numeric and string
constraints, usage, and supported JSON encodings. The added format regression first reproduced
acceptance of a malformed UUID and now rejects it and invalid timestamps. It took 0.04 seconds on
Linux ARM64 and peaked at 12,328,960 bytes in a 32 MiB container with no swap.

This boundary validates responses only. Outbound schema approval,
transport, and domain-specific command validation have separate gates.
