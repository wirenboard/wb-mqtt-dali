from wb.mqtt_dali.utils import (
    deep_merge_dicts,
    merge_json_schema_properties,
    merge_json_schemas,
    merge_translations,
)


class TestDeepMergeDicts:
    def test_nested_dicts_merge_keeping_unmentioned_dst_keys(self):
        dst = {"instance0": {"event_priority": 4, "event_scheme": 1, "addr": 2}}
        src = {"instance0": {"event_priority": 5}}
        deep_merge_dicts(dst, src)
        assert dst["instance0"] == {"event_priority": 5, "event_scheme": 1, "addr": 2}

    def test_empty_nested_src_does_not_wipe_dst(self):
        dst = {"instance1": {"event_priority": 4, "event_scheme": 1}}
        src = {"instance1": {}}
        deep_merge_dicts(dst, src)
        assert dst["instance1"] == {"event_priority": 4, "event_scheme": 1}

    def test_scalar_in_src_replaces_dst_value(self):
        dst = {"shared": "from_h1"}
        src = {"shared": "from_h2"}
        deep_merge_dicts(dst, src)
        assert dst["shared"] == "from_h2"

    def test_partial_nested_delta_preserves_sibling_keys(self):
        dst = {
            "instance0": {"event_priority": 4, "event_scheme": 1},
            "instance1": {"event_priority": 4, "event_scheme": 1},
            "instance2": {"event_priority": 4, "event_scheme": 1},
        }
        src = {"instance0": {"event_priority": 5}, "instance1": {}, "instance2": {}}
        deep_merge_dicts(dst, src)
        assert dst["instance0"] == {"event_priority": 5, "event_scheme": 1}
        assert dst["instance1"] == {"event_priority": 4, "event_scheme": 1}
        assert dst["instance2"] == {"event_priority": 4, "event_scheme": 1}

    def test_list_is_replaced_wholesale(self):
        dst = {"scenes": [1, 2, 3]}
        src = {"scenes": [9]}
        deep_merge_dicts(dst, src)
        assert dst["scenes"] == [9]

    def test_dict_in_src_replaces_non_dict_in_dst(self):
        dst = {"feedback": None}
        src = {"feedback": {"event_priority": 4}}
        deep_merge_dicts(dst, src)
        assert dst["feedback"] == {"event_priority": 4}

    def test_new_key_is_added(self):
        dst = {"a": 1}
        src = {"b": 2}
        deep_merge_dicts(dst, src)
        assert dst == {"a": 1, "b": 2}


class TestMergeTranslations:
    def test_merge_translations_empty_dst(self):
        dst = {}
        src = {"translations": {"ru": {"key": "значение"}, "en": {"key": "value"}}}
        merge_translations(dst, src)
        assert dst == {"translations": {"ru": {"key": "значение"}, "en": {"key": "value"}}}

    def test_merge_translations_ru_only(self):
        dst = {"translations": {"ru": {"existing": "данные"}}}
        src = {"translations": {"ru": {"key": "значение"}}}
        merge_translations(dst, src)
        assert dst["translations"]["ru"] == {"existing": "данные", "key": "значение"}

    def test_merge_translations_en_only(self):
        dst = {}
        src = {"translations": {"en": {"key": "value"}}}
        merge_translations(dst, src)
        assert dst["translations"]["en"] == {"key": "value"}

    def test_merge_translations_both_languages(self):
        dst = {"translations": {"ru": {"a": "1"}}}
        src = {"translations": {"ru": {"b": "2"}, "en": {"c": "3"}}}
        merge_translations(dst, src)
        assert dst["translations"]["ru"] == {"a": "1", "b": "2"}
        assert dst["translations"]["en"] == {"c": "3"}

    def test_merge_translations_no_translations_in_src(self):
        dst = {"translations": {"ru": {"key": "значение"}}}
        src = {}
        merge_translations(dst, src)
        assert dst == {"translations": {"ru": {"key": "значение"}}}


class TestMergeJsonSchemaProperties:
    def test_merge_properties_empty_dst(self):
        dst = {}
        src = {"properties": {"name": {"type": "string"}}}
        merge_json_schema_properties(dst, src)
        assert dst == {"properties": {"name": {"type": "string"}}}

    def test_merge_properties_existing(self):
        dst = {"properties": {"id": {"type": "integer"}}}
        src = {"properties": {"name": {"type": "string"}}}
        merge_json_schema_properties(dst, src)
        assert dst["properties"] == {"id": {"type": "integer"}, "name": {"type": "string"}}

    def test_merge_required_empty_dst(self):
        dst = {}
        src = {"required": ["name", "id"]}
        merge_json_schema_properties(dst, src)
        assert dst["required"] == ["name", "id"]

    def test_merge_required_no_duplicates(self):
        dst = {"required": ["id"]}
        src = {"required": ["id", "name"]}
        merge_json_schema_properties(dst, src)
        assert dst["required"] == ["id", "name"]

    def test_merge_required_preserves_order(self):
        dst = {"required": ["id"]}
        src = {"required": ["name", "email"]}
        merge_json_schema_properties(dst, src)
        assert dst["required"] == ["id", "name", "email"]

    def test_merge_properties_and_required(self):
        dst = {"properties": {"id": {"type": "integer"}}, "required": ["id"]}
        src = {"properties": {"name": {"type": "string"}}, "required": ["name"]}
        merge_json_schema_properties(dst, src)
        assert dst["properties"] == {"id": {"type": "integer"}, "name": {"type": "string"}}
        assert dst["required"] == ["id", "name"]


class TestMergeJsonSchemas:
    def test_merge_full_schemas(self):
        dst = {"properties": {"id": {"type": "integer"}}, "translations": {"ru": {"id": "Идентификатор"}}}
        src = {"properties": {"name": {"type": "string"}}, "translations": {"en": {"name": "Name"}}}
        merge_json_schemas(dst, src)
        assert dst["properties"] == {"id": {"type": "integer"}, "name": {"type": "string"}}
        assert dst["translations"]["ru"] == {"id": "Идентификатор"}
        assert dst["translations"]["en"] == {"name": "Name"}

    def test_merge_empty_dst_schema(self):
        dst = {}
        src = {
            "properties": {"name": {"type": "string"}},
            "translations": {"en": {"name": "Name"}},
            "required": ["name"],
        }
        merge_json_schemas(dst, src)
        assert "properties" in dst
        assert "translations" in dst
        assert "required" in dst
