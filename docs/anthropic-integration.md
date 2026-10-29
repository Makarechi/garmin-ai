# Anthropic Messages integration (experimental)

The optional Anthropic adapter sends structured text requests to the native
`https://api.anthropic.com/v1/messages` endpoint using API version `2026-10-21`.
It uses the shared instance-scoped consent and request gate. Only one model
instance may be enabled in a runtime. The model and structured-output capability
must be declared explicitly; this declaration does not prove live compatibility.

Configure `GA_LLM_ENABLED=true`, a matching `GA_LLM_CONSENT`, and an explicit
`GA_INTEGRATIONS` model instance. Example shapes, with fictional identifiers:

```json
{"id":"model:anthropic:primary","kind":"model","provider":"anthropic","enabled":true,
 "config":{"model":"YOUR_SUPPORTED_MODEL","structured_output":true},
 "secret_refs":{"api_key":"GA_PLUGIN_ANTHROPIC_API_KEY"}}
```

```json
{"provider":"anthropic","provider_instance_id":"model:anthropic:primary",
 "model":"YOUR_SUPPORTED_MODEL","categories":["health","diary"],
 "granted_at":"2026-01-01T00:00:00Z","policy_revision":1}
```

Provide `GA_PLUGIN_ANTHROPIC_API_KEY` through the deployment's secret mechanism,
not in integration JSON, a repository file or a bot message. Consent must cover
that exact instance, model and requested categories; onboarding may further
restrict them. Changing the model or secret requires a new client. There is no
automatic provider fallback or request retry. The adapter advertises structured
text only; voice transcription and native tool use are not supported. The
existing bounded planner selects application tools through structured fields.

The provider's [structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
says schemas can be cached separately from message content. Accordingly the
adapter sends only trusted application-owned schemas and never generates schema
property names, enum values, constants or patterns from private tracker data.
It omits unsupported schema constraints and validates the result against the
original application model. Consult the provider's data-retention terms for
the selected account and model; no blanket retention guarantee is implied.

`tests/test_anthropic_live.py` is a synthetic smoke check. It runs only with
`GA_LIVE_ANTHROPIC_TESTS=1`, an explicit `GA_ANTHROPIC_LIVE_MODEL`, and
`GA_PLUGIN_ANTHROPIC_LIVE_KEY` supplied as a secret. A passing smoke check is not
full model or multilingual acceptance. No live test has been performed as part
of these changes.

## Local synthetic HTTP checks

With Postman CLI 1.76 or newer, run a local simulation and invoke its probe in a
separate terminal. Only fictional request data and keys are used.

```sh
postman simulation run postman/simulations/anthropic-messages.sim.yaml --port 57105 --no-history --output ndjson
PYTHONPATH=src python tests/evals/anthropic_simulation_probe.py http://127.0.0.1:57105/anthropic-messages/v1/messages
```

For failures, substitute `anthropic-rate-limit.sim.yaml` or
`anthropic-timeout.sim.yaml` and pass `--expect rate-limit` or `--expect timeout`.
The success log must show the request to `/v1/messages`. The stand-in does not
prove real model compatibility or exercise the production TLS path. See the
per-scenario context files for assumptions and observations.
