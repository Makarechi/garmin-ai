from garmin_ai.metric_definitions import CoveragePolicy, MetricSpec
from garmin_ai.tracker_forms import TrackerFieldDraft


def test_generated_scale_unit_accepts_any_bounded_integer_range():
    TrackerFieldDraft(
        key="effort", label="Effort", kind="scale", minimum=0, maximum=10
    )
    spec = MetricSpec(
        key="user.training.effort",
        labels={"en": "Effort"},
        value_kind="ordinal",
        unit="score_0-10",
        dimension="ordinal",
        scale_id="user.training.effort",
        scale_version=1,
        aggregation="median",
        allowed_methods={"latest", "median", "distribution"},
        coverage=CoveragePolicy(kind="all_values"),
        time_semantics="point",
        minimum=0,
        maximum=10,
    )

    assert spec.unit == "score_0-10"
