def merge_json_schemas(dst: dict, src: dict) -> None:
    merge_json_schema_properties(dst, src)
    merge_translations(dst, src)


def merge_translations(dst: dict, src: dict) -> None:
    if "translations" in src:
        if "translations" not in dst:
            dst["translations"] = {}
        if "ru" in src["translations"]:
            if "ru" not in dst["translations"]:
                dst["translations"]["ru"] = {}
            dst["translations"]["ru"].update(src["translations"]["ru"])
        if "en" in src["translations"]:
            if "en" not in dst["translations"]:
                dst["translations"]["en"] = {}
            dst["translations"]["en"].update(src["translations"]["en"])


def merge_json_schema_properties(dst: dict, src: dict) -> None:
    if "properties" not in dst:
        dst["properties"] = {}
    if "properties" in src:
        dst["properties"].update(src["properties"])
