"""The common device schema from the repository, as `main` passes it to the gateway."""

import json
from pathlib import Path

_COMMON_SCHEMA_RELATIVE_PATH = Path("schemas") / "common_device.schema.json"


def load_common_schema() -> dict:
    # Under pybuild the tests run from a build copy, the schema stays in the source root above it.
    for directory in Path(__file__).resolve().parents:
        candidate = directory / _COMMON_SCHEMA_RELATIVE_PATH
        if candidate.is_file():
            with open(candidate, "r", encoding="utf-8") as schema_file:
                return json.load(schema_file)
    raise FileNotFoundError(f"{_COMMON_SCHEMA_RELATIVE_PATH} not found above {__file__}")
