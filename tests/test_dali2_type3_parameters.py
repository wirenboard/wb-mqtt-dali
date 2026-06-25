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


def _description_key(property_name: str, desc: TranslatedTitle) -> str:
    param = NumberSettingsParam(SettingsParamName("Name", "Имя"), property_name)
    param.description = desc
    return param.get_schema(group_and_broadcast=False)["properties"][property_name]["description"]


def test_description_key_derived_from_text_not_class_or_property():
    """The description key is content-addressed: report_timer / ReportTimerParam
    recur across instance types 2/3/4/6, so neither the property_name nor the class
    identifies the text. Identical descriptions dedup to one key; a differing en or
    a differing ru each yields a distinct key — preventing silent collisions in the
    flat, device-wide translations map."""
    base = TranslatedTitle(en="Report interval.", ru="Период отчёта.")
    assert _description_key("report_timer", base) == _description_key("report_timer", base)

    other_en = _description_key("report_timer", TranslatedTitle(en="Other.", ru="Период отчёта."))
    other_ru = _description_key("report_timer", TranslatedTitle(en="Report interval.", ru="Другое."))
    assert len({_description_key("report_timer", base), other_en, other_ru}) == 3


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
