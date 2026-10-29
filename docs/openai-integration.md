# OpenAI Responses integration (experimental)

The optional OpenAI adapter sends structured text requests to the native
`https://api.openai.com/v1/responses` endpoint. It uses the existing instance-scoped
consent and request gate; enabling it does not authorize Gemini or another
provider. Only one model instance may be enabled in a runtime. The model and its
structured-output capability must be configured explicitly. A declaration is not
a live compatibility check: use a model that supports the supplied schemas.

Configure `GA_LLM_ENABLED=true`, a matching `GA_LLM_CONSENT`, and an explicit
`GA_INTEGRATIONS` model instance. Example shapes, with fictional identifiers:

```json
{"id":"model:openai:primary","kind":"model","provider":"openai","enabled":true,
 "config":{"model":"YOUR_SUPPORTED_MODEL","structured_output":true},
 "secret_refs":{"api_key":"GA_PLUGIN_OPENAI_API_KEY"}}
```

```json
{"provider":"openai","provider_instance_id":"model:openai:primary",
 "model":"YOUR_SUPPORTED_MODEL","categories":["health","diary"],
 "granted_at":"2026-01-01T00:00:00Z","policy_revision":1}
```

Provide `GA_PLUGIN_OPENAI_API_KEY` through the deployment's secret mechanism,
not in the integration JSON, a repository file or a bot message. Consent must
cover the exact instance, model and requested data categories; onboarding
choices can further restrict them. Changing a model or secret requires a new
client. The adapter makes one request per attempt and never falls back to a
second provider. It supports structured text only: voice transcription and
native function calling are not advertised. The existing bounded planner uses
structured response fields to select application tools.

The adapter sends `store=false`. This is not a promise of zero provider
retention; review the [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
for the account and workload. Only approved application-owned response schemas
are sent; descriptions and defaults are removed, and the original schema and
application validation still apply to received values. Local tests use synthetic
inputs. No real model or language is considered accepted without a separate
opt-in evaluation.

An initial live smoke test is available in `tests/test_openai_live.py`. It runs
only when `GA_LIVE_OPENAI_TESTS=1`, `GA_OPENAI_LIVE_MODEL` names the chosen
model, and `GA_PLUGIN_OPENAI_LIVE_KEY` is supplied through a secret mechanism.
It sends a fictional message and records the configured model and prompt/schema
hashes in a JUnit report when one is requested. A passed smoke test is not the
full language, workflow or model acceptance described in the project plan.

## Local synthetic HTTP checks

With Postman CLI 1.76 or newer, run one of the three local simulations and invoke
its probe in a separate terminal. Only fictional request data and keys are used.

```sh
postman simulation run postman/simulations/openai-responses.sim.yaml --port 57102 --no-history --output ndjson
PYTHONPATH=src python tests/evals/openai_simulation_probe.py http://127.0.0.1:57102/openai-responses/v1/responses
```

For the failure cases, substitute `openai-rate-limit.sim.yaml` or
`openai-timeout.sim.yaml` and pass `--expect rate-limit` or `--expect timeout`.
Check the simulation request event for the success case. The stand-in does not
prove that a real model supports these schemas or that the production TLS path
works. Per-scenario context files record the assumptions and verification.
