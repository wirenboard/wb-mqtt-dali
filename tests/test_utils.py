from wb.mqtt_dali.utils import (
    merge_json_schema_properties,
    merge_json_schemas,
    merge_translations,
)


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
