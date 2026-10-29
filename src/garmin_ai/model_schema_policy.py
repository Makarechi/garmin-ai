"""Allow only application-owned schemas in external model metadata."""


def trusted_model_schema(schema):
    # Tracker labels and options may contain private information. Only fixed
    # application classes can supply property names, enum values and patterns.
    from garmin_ai.agent import AgentStep, Interpretation, SafetyScreen
    from garmin_ai.extension_tck import ModelProbe
    from garmin_ai.natural_language import TrackerExtraction

    return schema in {AgentStep, Interpretation, SafetyScreen, TrackerExtraction, ModelProbe}
