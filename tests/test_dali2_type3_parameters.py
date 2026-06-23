from dali.address import InstanceNumber

from wb.mqtt_dali.dali2_type3_parameters import build_type3_occupancy_sensor_parameters
from wb.mqtt_dali.dali2_type32_parameters import ActiveFeedbackColourParam


def test_description_translated_to_both_locales():
    """Each type-3 occupancy param (hold/report/deadtime) exposes a short
    per-property description key, and registers both the en and ru variants of
    the long text under that key, alongside the title translation."""
    params = build_type3_occupancy_sensor_parameters(InstanceNumber(0))
    for param in params:
        schema = param.get_schema(group_and_broadcast=False)
        description_key = schema["properties"][param.property_name]["description"]
        assert description_key == f"{param.property_name}_description"
        # Title translation must survive next to the description translation.
        assert schema["translations"]["ru"][param.name.en] == param.name.ru
        # The long texts live in translations, keyed by the short key — not inlined.
        assert schema["translations"]["en"][description_key].strip()
        assert schema["translations"]["ru"][description_key].strip()


def test_description_without_ru_adds_only_en_translation():
    """A param whose description is a TranslatedTitle with no ru variant (type32
    colour param) yields a valid schema: the en translation is registered under
    the description key, but no ru translation is added for it."""
    param = ActiveFeedbackColourParam(InstanceNumber(0))
    schema = param.get_schema(group_and_broadcast=False)
    description_key = schema["properties"][param.property_name]["description"]
    assert description_key == f"{param.property_name}_description"
    assert schema["translations"]["en"][description_key].strip()
    assert description_key not in schema.get("translations", {}).get("ru", {})
