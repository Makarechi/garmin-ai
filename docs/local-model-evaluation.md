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

On 2026-10-08, a further native Ollama 0.40.0 probe on the same Apple M4 Mac
tested `qwen3.5:9b-q4_K_M` and `qwen3.5:27b-q4_K_M` with the application's
extraction instruction and output schema, fictional input in the production
prompt shape, temperature zero, thinking disabled and a 16,384-token context.
Both models returned `caffeine_log_complete` for a simple one-cup coffee entry,
which fails domain validation because no covered interval was supplied and
misstates the user's intent. The warmed 9B response took 24 seconds; the first
27B request, including model load, took 198 seconds. The 27B model did ask for
clarification about an ambiguous "after lunch" time (58 seconds), and 9B
correctly declined to invent a count
when the fictional analysis context contained no entries (8 seconds). These
probes did not run the full application flow or validate a release. The basic
recording failure alone keeps both configurations unsupported.

The same native setup also probed
[`mistral-small3.2:24b`](https://ollama.com/library/mistral-small3.2) with the
same fictional coffee entry, instruction, schema and decoding settings. Its
first request, including model load, took 111 seconds. It invented a separate
"no coffee" event and classified the reported cup as a completed caffeine log;
both events failed interval validation. This candidate remains unsupported and
was not run through the full application flow.
