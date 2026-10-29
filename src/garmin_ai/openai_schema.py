"""OpenAI Structured Outputs schema projection."""

from garmin_ai.structured_schema import ClosedSchemaCompiler, UnsupportedSchema


class OpenAISchemaCompiler(ClosedSchemaCompiler):
    pass


__all__ = ["OpenAISchemaCompiler", "UnsupportedSchema"]
