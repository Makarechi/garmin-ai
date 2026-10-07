# Local Ollama model evaluation

The CP-08 experiment did not produce a supported backend. No Ollama adapter
or model configuration is shipped for personal diary use: the synthetic
creation, clarification and analysis acceptance flow has not passed.

On 2026-10-07, local checks on an Apple M4 Mac with 32 GiB of RAM tried
qwen3:4b, qwen3:4b-instruct, qwen3:8b, qwen3:14b, gpt-oss:20b,
gemma3:12b and gemma3:27b. Smaller models produced invalid, empty or
incorrect structured answers for fictional diary and analysis prompts.
gemma3:12b extracted one synthetic entry correctly but returned no valid
analysis call. gemma3:27b timed out after 180 seconds on a simple analysis
question. Mock transport and consent tests passed, but they do not establish
real model behavior. No original health data was used.

The archived prototype limited endpoints to local hosts and did not fall back
to a cloud model. Those guards did not establish answer quality. Before
shipping an adapter, select a specific model and hardware configuration, pass
the same end-to-end fictional creation, clarification and analysis stories as
the existing backend, and check wrong schemas, invented numbers, timeouts,
large replies and revoked consent without diary mutation. Record latency and
resource use for that tested configuration before publishing setup instructions
or capability claims.
