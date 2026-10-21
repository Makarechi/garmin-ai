from string import Formatter

from garmin_ai.events import headache_observation_label, medication_label
from garmin_ai.i18n import catalog, translate


def test_agent_and_freshness_translations_have_matching_keys_and_placeholders():
    english = catalog("en")
    russian = catalog("ru")
    keys = {key for key in english if key.startswith(("agent.", "freshness."))}

    assert keys == {key for key in russian if key.startswith(("agent.", "freshness."))}
    for key in keys:
        en_fields = {field for _, field, _, _ in Formatter().parse(english[key]) if field}
        ru_fields = {field for _, field, _, _ in Formatter().parse(russian[key]) if field}
        assert en_fields == ru_fields, key


def test_unknown_language_falls_back_to_english_agent_message():
    assert translate("agent.record_fallback", "de-DE") == translate("agent.record_fallback", "en")


def test_saved_record_labels_follow_message_language():
    assert medication_label({"name": None, "dose": None, "unit": None}, "en") == (
        "Medication: name unknown, dose unknown unit unknown"
    )
    assert headache_observation_label({"headache": "yes", "migraine": "no"}, "en") == (
        "Headache: yes; migraine: no"
    )
