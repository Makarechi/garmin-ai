# Bounded native analysis read loop

The native model gateway uses the existing analysis instruction and shares the interpretation budget with the answer. The loop serializes prompts in the same insertion order and with the same separators used by Python. It counts prompt and evidence bytes in UTF-8, bounds preparation buffers, and reserves the last round for an answer.

A trusted adapter offers the available reads and checks the context before another read, model request or answer. Only offered names and JSON object arguments reach that adapter. Failed or rejected calls cannot provide evidence for a final answer. The loop returns the verified narrative, claims and selected source results for the channel's final reply.

The native tests used in-process model and reader responses. They exercised a hidden write request, malformed read arguments, a verified number from the allowed read, a context change, and an oversized evidence result. Both passed on Linux ARM64, peaking at 16,752,640 bytes under 32 MiB without swap. The concrete owner-scoped read adapter and delivery of the resulting answer are separate integration work.

The trusted reader reports storage-derived tracker versions separately from tool JSON. Later model attempts recheck permission for all such facts, and the verified result carries the dependencies for the reply path. The fixture confirms that model messages do not contain this metadata.
