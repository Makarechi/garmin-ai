"""Exercise the comparator using the captured Python fixture and targeted mutations."""

import unittest
from copy import deepcopy
from pathlib import Path

from differential import compare, load, parse_float, reject_nonfinite, unique_object

REFERENCE = Path(__file__).parent / "contracts/python-initial-observation.json"


class DifferentialTests(unittest.TestCase):
    def setUp(self):
        self.reference = load(REFERENCE)
        self.candidate = deepcopy(self.reference)

    def test_metadata_identifies_each_implementation_without_becoming_a_product_difference(self):
        self.candidate["implementation"] = {"language": "rust", "revision": "test-candidate"}
        self.assertEqual(compare(self.reference, self.candidate), [])

    def test_missing_metric_replaced_with_zero_is_detected(self):
        archive = self.candidate["scenarios"]["archive"]
        archive["missing_metric"] = archive["zero_metric"]
        changes = compare(self.reference, self.candidate)
        self.assertTrue(any("/missing_metric/bytes_base64" in item for item in changes))

    def test_duplicate_event_is_detected(self):
        diary = self.candidate["scenarios"]["diary"]
        diary["events"].append(deepcopy(diary["events"][0]))
        diary["row_counts"]["events"] += 1
        changes = compare(self.reference, self.candidate)
        self.assertIn("/scenarios/diary/events: length 1 became 2", changes)
        self.assertIn("/scenarios/diary/row_counts/events: value differs", changes)

    def test_wrong_revision_is_detected(self):
        self.candidate["scenarios"]["diary"]["responses"]["retry"]["body"]["revision"] = 7
        self.assertIn(
            "/scenarios/diary/responses/retry/body/revision: value differs",
            compare(self.reference, self.candidate),
        )

    def test_wrong_archive_hash_is_detected(self):
        self.candidate["scenarios"]["archive"]["unicode_and_numbers"]["key"] = "ff/wrong.json"
        self.assertIn(
            "/scenarios/archive/unicode_and_numbers/key: value differs",
            compare(self.reference, self.candidate),
        )

    def test_comparison_keeps_boolean_null_presence_and_signed_zero_distinct(self):
        self.reference["scenarios"] = {"value": [True, None, -0.0, {}]}
        self.candidate["scenarios"] = {"value": [1, 0, 0.0, {"reading/1~2": 0}]}
        self.assertEqual(
            compare(self.reference, self.candidate),
            [
                "/scenarios/value/0: type bool became int",
                "/scenarios/value/1: type NoneType became int",
                "/scenarios/value/2: value differs",
                "/scenarios/value/3/reading~11~02: unexpected",
            ],
        )

    def test_replay_preserves_archive_identity_and_failed_transaction_boundary(self):
        replay = self.reference["scenarios"]["replay"]
        half = replay["after_interrupted_transaction"]
        self.assertEqual(half["database_source_count"], 0)
        self.assertIsNone(half["daily_steps"])
        self.assertEqual(len(half["archive_keys"]), 1)
        requests = replay["requests"]
        self.assertEqual([row["current_steps"] for row in requests], [10, 20, 10])
        self.assertEqual(requests[0]["source_ref"], requests[2]["source_ref"])
        self.assertNotEqual(requests[0]["source_ref"], requests[1]["source_ref"])
        final = replay["after_replay"]
        self.assertEqual(final["database_source_count"], 2)
        self.assertEqual(final["daily_steps"], 10)
        candidate = self.candidate["scenarios"]["replay"]
        candidate["requests"][2]["source_ref"] = requests[1]["source_ref"]
        self.assertIn(
            "/scenarios/replay/requests/2/source_ref: value differs",
            compare(self.reference, self.candidate),
        )

    def test_rejects_invalid_json_contract_values(self):
        with self.assertRaises(ValueError):
            parse_float("1e999")
        with self.assertRaises(ValueError):
            reject_nonfinite("NaN")
        with self.assertRaises(ValueError):
            unique_object([("value", 1), ("value", 2)])


if __name__ == "__main__":
    unittest.main()
