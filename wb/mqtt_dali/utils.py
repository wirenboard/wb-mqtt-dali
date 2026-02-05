def merge_json_schemas(dst: dict, src: dict) -> None:
    merge_json_schema_properties(dst, src)
    merge_translations(dst, src)


def merge_translations(dst: dict, src: dict) -> None:
    if "translations" in src:
        if "ru" in src["translations"]:
            dst_translations = dst.setdefault("translations", {}).setdefault("ru", {})
            dst_translations.update(src["translations"]["ru"])
        if "en" in src["translations"]:
            dst_translations = dst.setdefault("translations", {}).setdefault("en", {})
            dst_translations.update(src["translations"]["en"])


def merge_json_schema_properties(dst: dict, src: dict) -> None:
    if "properties" in src:
        properties = dst.setdefault("properties", {})
        properties.update(src["properties"])
    if "required" in src:
        required = dst.setdefault("required", [])
        for req in src["required"]:
            if req not in required:
                required.append(req)


def add_translations(schema: dict, lang: str, translations: dict[str, str]) -> None:
    translations_dict = schema.setdefault("translations", {}).setdefault(lang, {})
    translations_dict.update(translations)


def add_enum(schema: dict, enum_values: list[tuple[int, str]]) -> None:
    schema["enum"] = []
    options = schema.setdefault("options", {})
    options["enum_titles"] = []
    for value, title in enum_values:
        schema["enum"].append(value)
        options["enum_titles"].append(title)
