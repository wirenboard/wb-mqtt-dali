def merge_json_schemas(dst: dict, src: dict) -> dict:
    if "properties" not in dst:
        dst["properties"] = {}
    dst["properties"].update(src["properties"])
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
    return dst
