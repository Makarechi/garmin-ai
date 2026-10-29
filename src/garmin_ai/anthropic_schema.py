"""Anthropic Messages structured-output schema projection."""

from pydantic import BaseModel

from garmin_ai.structured_schema import ClosedSchemaCompiler, UnsupportedSchema


class AnthropicSchemaCompiler(ClosedSchemaCompiler):
    # Anthropic does not support numeric constraints or most array limits.
    # Keep these constraints in the original server-side model instead.
    _scalar_keys = ("type", "enum")
    _formats = ClosedSchemaCompiler._formats | {"uri"}
    _allow_recursive = False

    def compile(self, schema: type[BaseModel]) -> dict:
        original = schema.model_json_schema()
        compiled = super().compile(schema)

        def count_unions(node):
            if isinstance(node, list):
                return sum(count_unions(value) for value in node)
            if not isinstance(node, dict):
                return 0
            total = int("anyOf" in node)
            for key, value in node.items():
                if key in {"properties", "$defs"}:
                    total += sum(count_unions(child) for child in value.values())
                else:
                    total += count_unions(value)
            return total

        # A missing field with a None default has the same meaning to Pydantic
        # as an explicit null. Use that equivalence only when necessary to fit
        # Anthropic's grammar limits, keeping validation by the original model.
        remaining = max(0, count_unions(compiled) - 16)
        if remaining > 24:
            raise UnsupportedSchema("Anthropic schema exceeds complexity limits")

        def simplify(source, target):
            nonlocal remaining
            if not isinstance(source, dict) or not isinstance(target, dict):
                return
            for name, field in source.get("properties", {}).items():
                projected = target["properties"][name]
                choices = field.get("anyOf", [])
                nulls = [i for i, choice in enumerate(choices) if choice == {"type": "null"}]
                if (
                    remaining
                    and name not in source.get("required", [])
                    and "default" in field
                    and field["default"] is None
                    and len(choices) == 2
                    and len(nulls) == 1
                    and set(projected) == {"anyOf"}
                ):
                    index = 1 - nulls[0]
                    projected = projected["anyOf"][index]
                    target["properties"][name] = projected
                    target["required"].remove(name)
                    remaining -= 1
                    simplify(choices[index], projected)
                else:
                    simplify(field, projected)
            for source_key in ("anyOf", "oneOf"):
                if source_key in source:
                    for choice, projected in zip(
                        source[source_key], target.get("anyOf", []), strict=True
                    ):
                        simplify(choice, projected)
            if "items" in source:
                simplify(source["items"], target.get("items"))

        root = original
        while "$ref" in root:
            root = original["$defs"][root["$ref"][8:]]
        simplify(root, compiled)
        for name, definition in original.get("$defs", {}).items():
            simplify(definition, compiled["$defs"][name])
        if remaining or count_unions(compiled) > 16:
            raise UnsupportedSchema("Anthropic schema exceeds complexity limits")
        return compiled


__all__ = ["AnthropicSchemaCompiler", "UnsupportedSchema"]
