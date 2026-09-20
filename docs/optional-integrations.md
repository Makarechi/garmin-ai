# Optional integrations

The local diary, deterministic forms, API, analytics, and export are the core product. Garmin,
Telegram, and Gemini are optional integration instances with stable IDs:

- `source:garmin:primary`
- `channel:telegram:primary`
- `model:gemini:primary`

Existing installations need no configuration change. When `GA_INTEGRATIONS` is absent, the
current credentials and token directory select the same legacy integrations and IDs. A deployment
that sets `GA_INTEGRATIONS` opts into an explicit allowlist: omitted or disabled instances do not
start. Secrets remain in their existing secret settings and are never stored in this list.

Install only the needed SDK with `uv sync --extra garmin`, `--extra telegram`, or
`--extra gemini`. `uv sync --extra full` preserves the existing complete deployment and is used by
the Docker image. Missing SDKs produce an unavailable capability status without disabling local
operations.

Model consent is scoped to both the provider and its stable instance ID. Changing the model,
instance, or requested data categories requires matching consent; it is not inherited silently.
Every adapter advertises only capabilities it actually supports. Callers must treat an unavailable
or unsupported capability as an explicit result instead of assuming data exists.
