# Reply safety screening

The native gateway uses the existing reply and fragment safety prompts. A normal reply is screened in one request. Longer input uses four requests at most, with the existing 12,000-code-point fragments and 256-code-point overlap. The gateway checks the approved health and diary permissions for each request, and each intermediate response can update the language of the final notice.

An urgent result stops screening. If a long screen fails or leaves a tail outside its supported size, it returns the existing conservative outcome for the caller to render.

The ARM64 test compared ten decisions and fingerprints for each complete prompt and text fragment with pinned Python. It exercised multibyte text, exact size limits, urgency in a later fragment and partial screening during a provider outage. The 32 MiB test peaked at 11,472,896 bytes with no swap.
