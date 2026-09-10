# External model consent (GA-24, first phase)

`GA_LLM_ENABLED=true` alone no longer permits external model requests. The default
`GA_LLM_CONSENT=null` keeps them unavailable. Deterministic local diary and read tools
remain available without a model. This changes configuration requirements for existing
model-enabled installations; set consent deliberately before restarting the worker.

Before opting in, review the selected provider/model's current terms for your project,
region and billing mode. This technical consent record does not establish that a health
workflow is permitted by those terms. No provider switch or consent is automatic.

The current structured workflow can send user text, bounded diary records, requested
health summaries, timestamps and tool evidence. It therefore requires both `health`
and `diary`. `audio` separately permits original voice bytes for transcription; it is
not implied by consent to text. Fine-grained redaction and local transcription remain
future phases. Credentials and the consent record are not inserted into prompts.

After reviewing that scope, an owner can record consent in local configuration:

```dotenv
GA_LLM_CONSENT={"provider":"gemini","model":"YOUR_SELECTED_MODEL","categories":["health","diary"],"granted_at":"YOUR_CURRENT_ISO_TIMESTAMP_WITH_OFFSET","policy_revision":1}
```

Replace the placeholders intentionally; the example is not executable consent.
`model` must exactly match `GA_GEMINI_MODEL`. Only add `audio` if you also authorize
voice transmission. Unknown categories/providers and naive timestamps are rejected.
Future-dated consent is not active. The record persists in local configuration across
restarts. Changing the model requires a matching newly reviewed record.

To revoke, set `GA_LLM_CONSENT=null` or `GA_LLM_ENABLED=false` and restart the worker.
Each structured/transcription boundary checks the current settings again, so an
already-created client cannot bypass revoked or mismatched consent in memory.

This phase implements the consent boundary, not a second provider, provider-terms audit,
category-aware context minimization, a consent UI or account-specific project attestation.
Tests use synthetic requests and a captured transport; they make no external model call.
