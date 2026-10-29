import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).parent / "evals" / "acceptance_matrix.py"
_SPEC = importlib.util.spec_from_file_location("acceptance_matrix", _PATH)
matrix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(matrix)


def record(**changes):
    result = {
        "case_id": "AT-01",
        "provider": "openai",
        "model": "fictional-model-v1",
        "language": "sk",
        "workflow": "clarify_time",
        "level": "synthetic",
        "outcome": "pass",
        "reason": None,
        "observed_at": "2026-01-02T12:00:00Z",
        "commit": "a" * 40,
        "prompt_sha256": "b" * 64,
        "schema_sha256": "c" * 64,
        "tools_sha256": "d" * 64,
    }
    result.update(changes)
    return result


def test_empty_matrix_never_claims_acceptance():
    report = matrix.summarize([])
    assert report["cases"] == [f"AT-{number:02d}" for number in range(1, 41)]
    assert report["acceptance"] == "not_assessed"
    assert len(report["cells"]) == 3 * 12 * 5
    assert all(cell["status"] == "unverified" and not cell["models"] for cell in report["cells"])


def test_simulated_and_live_results_and_models_stay_separate():
    report = matrix.summarize(
        [
            record(),
            record(level="live", outcome="skipped", reason="not_configured"),
            record(
                level="live", model="fictional-model-v2", outcome="fail", reason="validation_failed"
            ),
        ]
    )
    cell = next(
        c
        for c in report["cells"]
        if c["provider"] == "openai" and c["language"] == "sk" and c["workflow"] == "clarify_time"
    )
    first = cell["models"]["fictional-model-v1"]["AT-01"]
    second = cell["models"]["fictional-model-v2"]["AT-01"]
    assert first["synthetic"]["pass"] == 1
    assert first["live"]["pass"] == 0
    assert first["live"]["skipped"] == 1
    assert second["live"]["fail"] == 1
    assert cell["status"] == "results_recorded"
    assert report["acceptance"] == "not_assessed"


@pytest.mark.parametrize(
    "changes",
    [
        {"case_id": "AT-41"},
        {"language": "xx"},
        {"provider": "unknown"},
        {"outcome": "pass", "reason": "other"},
        {"outcome": "skipped", "reason": None},
        {"level": []},
        {"model": ""},
        {"commit": "bad"},
        {"schema_sha256": "bad"},
        {"observed_at": "2026-01-01T12:00:00"},
        {"raw_prompt": "private text"},
    ],
)
def test_incomplete_or_unsanitized_records_are_rejected(changes):
    with pytest.raises(ValueError):
        matrix.summarize([record(**changes)])
