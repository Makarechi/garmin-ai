"""Compile application response schemas into OpenAI's strict JSON subset.

The compiled schema is a transport constraint. The original Pydantic model must
still validate every response, including constraints not expressible on the wire.
"""

from pydantic import BaseModel


class UnsupportedSchema(ValueError):
    """The input cannot be represented without discarding its structure."""


class OpenAISchemaCompiler:
    _scalar_keys = (
        "type",
        "enum",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
    )
    _formats = frozenset(
        {"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"}
    )

    def compile(self, schema: type[BaseModel]) -> dict:
        original = schema.model_json_schema()
        if original.get("type") != "object" or "properties" not in original:
            raise UnsupportedSchema("Structured output requires an object root")
        definitions = original.get("$defs", {})

        def convert(node):
            if not isinstance(node, dict):
                raise UnsupportedSchema("Invalid schema node")
            if not any(key in node for key in ("type", "$ref", "anyOf", "oneOf", "const", "enum")):
                raise UnsupportedSchema("Untyped schema values are not supported")
            if any(
                key in node for key in ("allOf", "not", "if", "then", "else", "patternProperties")
            ):
                raise UnsupportedSchema("Unsupported schema composition")
            result = {key: node[key] for key in self._scalar_keys if key in node}
            if "format" in node and node["format"] in self._formats:
                result["format"] = node["format"]
            if "$ref" in node:
                ref = node["$ref"]
                if (
                    not isinstance(ref, str)
                    or not ref.startswith("#/$defs/")
                    or ref[8:] not in definitions
                ):
                    raise UnsupportedSchema("Unsupported schema reference")
                result["$ref"] = ref
            if "const" in node:
                result["enum"] = [node["const"]]
            choices = node.get("anyOf", node.get("oneOf"))
            if choices is not None:
                result["anyOf"] = [convert(choice) for choice in choices]
            if node.get("type") == "object" or "properties" in node:
                properties = node.get("properties")
                if not isinstance(properties, dict) or node.get(
                    "additionalProperties", False
                ) not in (False,):
                    raise UnsupportedSchema("Open dictionaries are not supported")
                result["type"] = "object"
                result["properties"] = {name: convert(value) for name, value in properties.items()}
                result["required"] = list(properties)
                result["additionalProperties"] = False
            if "items" in node:
                result["items"] = convert(node["items"])
            return result

        result = convert(original)
        if definitions:
            result["$defs"] = {name: convert(value) for name, value in definitions.items()}
        return result
