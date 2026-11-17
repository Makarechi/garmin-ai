# Native structured model response validation

The shared decoder checks the terminal envelope before interpreting the
model output. OpenAI reasoning and Anthropic thinking blocks carry no
result. A refusal, truncated response, unsupported tool block, or malformed
output cannot become an application value. A completed value must satisfy
both the provider contract and the full local JSON Schema. Optional usage
counters retain their strict integer bounds. Error messages contain no
response or prompt text.

Fifty-eight synthetic response bodies matched the pinned OpenAI and
Anthropic adapters using local mock transports. The matrix covers missing
or malformed output, refusal precedence, token limits, numeric and string
constraints, usage, and supported JSON encodings. It took 0.04 seconds on
Linux ARM64 and peaked at 12,382,208 bytes in a 32 MiB container with no swap.

This boundary validates responses only. Outbound schema approval,
transport, and domain-specific command validation have separate gates.
