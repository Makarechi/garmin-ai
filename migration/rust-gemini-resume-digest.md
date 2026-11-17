# Gemini fallback request identity

The native request builder can reproduce the pinned Python fallback digest before dispatch. The digest covers the ordered model list, data categories, original instructions and input, thinking level, timeout and schema. It follows the logical request passed to the provider gate, including the audio shape before the SDK wraps it for HTTP.

The digest is computed incrementally. It keeps no additional copy of the prompt, audio or full Python representation. The pinned Unicode table is generated from CPython rather than depending on the host's Unicode release. An exhaustive test confirms the behavior for every Unicode scalar.

Seven text and voice requests matched actual digests written by the pinned provider gate after an invalid answer and a fallback deadline. Changing model order changed the digest. Reordering categories or preparing the same operation for the next model preserved it. The native Linux ARM64 fixture peaked at 12,398,592 bytes in a 32 MiB container with no swap and networking disabled.
