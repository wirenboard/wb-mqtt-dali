from dali.address import InstanceNumber

from wb.mqtt_dali.dali2_type3_parameters import build_type3_occupancy_sensor_parameters
from wb.mqtt_dali.dali2_type32_parameters import ActiveFeedbackColourParam
from wb.mqtt_dali.settings import NumberSettingsParam, SettingsParamName
from wb.mqtt_dali.wbmqtt import TranslatedTitle


def test_description_translated_to_both_locales():
    """Each type-3 occupancy param (hold/report/deadtime) puts a short key in
    description and registers both the en and ru long texts under that key,
    alongside the title translation."""
    params = build_type3_occupancy_sensor_parameters(InstanceNumber(0))
    for param in params:
        schema = param.get_schema(group_and_broadcast=False)
        description_key = schema["properties"][param.property_name]["description"]
        # The key is a short stand-in, not the long text itself.
        assert param.property_name in description_key
        # Title translation must survive next to the description translation.
        assert schema["translations"]["ru"][param.name.en] == param.name.ru
        # Both locale texts live in translations, keyed by the short key.
        assert schema["translations"]["en"][description_key].strip()
        assert schema["translations"]["ru"][description_key].strip()


def test_description_key_namespaced_by_param_class():
    """Two params that share a property_name but are different classes get
    distinct description keys, so their texts never collide in the flat,
    device-wide translations map."""

    class FirstParam(NumberSettingsParam):
        pass

    class SecondParam(NumberSettingsParam):
        pass

    shared_property = "report_timer"
    first = FirstParam(SettingsParamName("First", "Первый"), shared_property)
    first.description = TranslatedTitle(en="First text", ru="Первый текст")
    second = SecondParam(SettingsParamName("Second", "Второй"), shared_property)
    second.description = TranslatedTitle(en="Second text", ru="Второй текст")

    first_key = first.get_schema(group_and_broadcast=False)["properties"][shared_property]["description"]
    second_key = second.get_schema(group_and_broadcast=False)["properties"][shared_property]["description"]
    assert first_key != second_key


def test_description_without_ru_adds_only_en_translation():
    """A param whose description is a TranslatedTitle with no ru variant (type32
    colour param) yields a valid schema: the en translation is registered under
    the description key, but no ru translation is added for it."""
    param = ActiveFeedbackColourParam(InstanceNumber(0))
    schema = param.get_schema(group_and_broadcast=False)
    description_key = schema["properties"][param.property_name]["description"]
    assert schema["translations"]["en"][description_key].strip()
    assert description_key not in schema.get("translations", {}).get("ru", {})


def test_empty_description_emits_no_key():
    """An empty TranslatedTitle (no en, no ru) must not leak a raw description
    key into the schema, which the UI would otherwise render verbatim."""
    param = NumberSettingsParam(SettingsParamName("Plain", "Простой"), "plain_param")
    param.description = TranslatedTitle()
    schema = param.get_schema(group_and_broadcast=False)
    assert "description" not in schema["properties"][param.property_name]
