"""Anthropic Messages structured-output schema projection."""

from garmin_ai.structured_schema import ClosedSchemaCompiler, UnsupportedSchema


class AnthropicSchemaCompiler(ClosedSchemaCompiler):
    # Anthropic does not support numeric constraints or most array limits.
    # Keep these constraints in the original server-side model instead.
    _scalar_keys = ("type", "enum")
    _formats = ClosedSchemaCompiler._formats | {"uri"}
    _allow_recursive = False


__all__ = ["AnthropicSchemaCompiler", "UnsupportedSchema"]
