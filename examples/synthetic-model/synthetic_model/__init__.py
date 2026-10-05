"""Deterministic external model fixture; never opens a network connection."""

import json
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from garmin_ai.integrations import IntegrationFactory, PluginContext


class SyntheticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=100)
    response: str = Field(default="none", pattern="^(none|clarify|create_entry)$")


class SyntheticProvider:
    instances = []

    def __init__(self, context: PluginContext):
        self.instance_id = context.instance_id
        self.config = context.config
        self.received_secret_names = frozenset(context.secrets)
        self.secret_fingerprints = {
            name: sha256(value.get_secret_value().encode()).hexdigest()
            for name, value in context.secrets.items()
        }
        self.calls = 0
        self.closed = False
        self.instances.append(self)

    def structured(self, _instruction, prompt, schema):
        self.calls += 1
        if "urgent" in schema.model_fields:
            return schema.model_validate({"urgent": False})
        if "intent" in schema.model_fields:
            if self.config.response == "create_entry":
                request = json.loads(prompt)
                text = request["text"]
                candidate = request["candidate_trackers"][0]

                def evidence(quote):
                    start = text.index(quote)
                    return {"start": start, "end": start + len(quote), "quote": quote}

                return schema.model_validate(
                    {
                        "schema_version": "tracker.nl.v1",
                        "intent": "create_entry",
                        "definition_version_id": candidate["definition_version_id"],
                        "start": "2026-09-20T19:00:00+02:00",
                        "end": "2026-09-20T19:15:00+02:00",
                        "start_evidence": evidence("2026-09-20 19:00"),
                        "end_evidence": evidence("2026-09-20 19:15"),
                        "fields": [
                            {
                                "field_id": "user.stretch.difficulty",
                                "value": 3,
                                "evidence": evidence("3"),
                            }
                        ],
                        "confidence": 0.99,
                    }
                )
            return schema.model_validate({"intent": self.config.response, "confidence": 1})
        raise ValueError("Synthetic model does not support this schema")

    def transcribe(self, _data, _mime_type):
        raise NotImplementedError("Synthetic model has no audio capability")

    def close(self):
        self.closed = True


descriptor = IntegrationFactory(
    kind="model",
    provider="synthetic",
    plugin_factory=SyntheticProvider,
    config_model=SyntheticConfig,
    capabilities=frozenset({"structured_output"}),
)
