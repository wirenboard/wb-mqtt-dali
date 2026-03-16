from unittest.mock import AsyncMock

import pytest
from dali.memory import oem
from dali.memory.location import FlagValue

from wb.mqtt_dali.dali_type50_parameters import (
    _FIELD_SPECS,
    Type50MemoryBankParam,
    Type50Parameters,
)


def _make_raw(overrides: dict) -> dict:
    """Build a minimal bank-1 raw dict with all values unknown (FlagValue.MASK),
    then apply overrides for specific oem fields."""
    base = {
        oem.ManufacturerGTIN: FlagValue.MASK,
        oem.LuminaireID: FlagValue.MASK,
        oem.YearOfManufacture: FlagValue.MASK,
        oem.WeekOfManufacture: FlagValue.MASK,
        oem.InputPowerNominal: FlagValue.MASK,
        oem.InputPowerMinimumDim: FlagValue.MASK,
        oem.MainsVoltageMinimum: FlagValue.MASK,
        oem.MainsVoltageMaximum: FlagValue.MASK,
        oem.LightOutputNominal: FlagValue.MASK,
        oem.CRI: FlagValue.MASK,
        oem.CCT: FlagValue.MASK,
        oem.LightDistributionType: FlagValue.MASK,
        oem.LuminaireColor: "",
        oem.LuminaireIdentification: "",
    }
    base.update(overrides)
    return base


@pytest.fixture
def param():
    return Type50MemoryBankParam()


@pytest.fixture
def mock_driver():
    d = AsyncMock()
    d.run_sequence = AsyncMock()
    return d


# ---------------------------------------------------------------------------
# read() – happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_populated_fields(param, mock_driver):
    raw = _make_raw(
        {
            oem.YearOfManufacture: 24,
            oem.WeekOfManufacture: 10,
            oem.InputPowerNominal: 18,
            oem.LightOutputNominal: 2000,
            oem.CRI: 80,
            oem.CCT: 4000,
            oem.LightDistributionType: "Type II",
            oem.LuminaireColor: "white",
            oem.LuminaireIdentification: "LUM-001",
        }
    )
    mock_driver.run_sequence.return_value = raw

    result = await param.read(mock_driver, short_address=1)

    info = result["luminaire_info"]
    assert info["year_of_manufacture"] == 24
    assert info["week_of_manufacture"] == 10
    assert info["nominal_input_power"] == 18
    assert info["nominal_light_output"] == 2000
    assert info["cri"] == 80
    assert info["cct"] == 4000
    assert info["light_distribution_type"] == "Type II"
    assert info["luminaire_color"] == "white"
    assert info["luminaire_identification"] == "LUM-001"


@pytest.mark.asyncio
async def test_read_skips_flag_values(param, mock_driver):
    """FlagValue.MASK (unknown) fields must be omitted from the result."""
    raw = _make_raw({oem.CRI: 90})
    mock_driver.run_sequence.return_value = raw

    result = await param.read(mock_driver, short_address=1)
    info = result["luminaire_info"]
    assert "cri" in info
    assert "year_of_manufacture" not in info
    assert "nominal_input_power" not in info


@pytest.mark.asyncio
async def test_read_skips_all_ff_gtin(param, mock_driver):
    """Unprogrammed GTIN (all bytes 0xFF) must be omitted."""
    all_ff_6 = (1 << 48) - 1
    raw = _make_raw({oem.ManufacturerGTIN: all_ff_6, oem.CRI: 80})
    mock_driver.run_sequence.return_value = raw

    result = await param.read(mock_driver, short_address=1)
    assert "luminaire_gtin" not in result["luminaire_info"]


@pytest.mark.asyncio
async def test_read_includes_valid_gtin(param, mock_driver):
    raw = _make_raw({oem.ManufacturerGTIN: 123456789012})
    mock_driver.run_sequence.return_value = raw

    result = await param.read(mock_driver, short_address=1)
    assert result["luminaire_info"]["luminaire_gtin"] == 123456789012


@pytest.mark.asyncio
async def test_read_skips_empty_string_fields(param, mock_driver):
    """Empty or null-only strings must be omitted."""
    raw = _make_raw({oem.LuminaireColor: "", oem.LuminaireIdentification: "\x00\x00", oem.CRI: 80})
    mock_driver.run_sequence.return_value = raw

    result = await param.read(mock_driver, short_address=1)
    assert "luminaire_color" not in result["luminaire_info"]
    assert "luminaire_identification" not in result["luminaire_info"]


@pytest.mark.asyncio
async def test_read_skips_cct_part209(param, mock_driver):
    """CCT returning 'Part 209 implemented' (a string) must be omitted since schema type is integer."""
    raw = _make_raw({oem.CCT: "Part 209 implemented", oem.CRI: 80})
    mock_driver.run_sequence.return_value = raw

    result = await param.read(mock_driver, short_address=1)
    assert "cct" not in result["luminaire_info"]


@pytest.mark.asyncio
async def test_read_returns_empty_dict_when_no_fields(param, mock_driver):
    mock_driver.run_sequence.return_value = _make_raw({})
    result = await param.read(mock_driver, short_address=1)
    assert result == {}


@pytest.mark.asyncio
async def test_read_raises_on_bank_error(param, mock_driver):
    from dali.exceptions import ResponseError

    mock_driver.run_sequence.side_effect = ResponseError("framing error")
    with pytest.raises(RuntimeError, match="Failed to read DT50 memory bank"):
        await param.read(mock_driver, short_address=1)


# ---------------------------------------------------------------------------
# get_schema()
# ---------------------------------------------------------------------------


def test_get_schema_contains_all_fields(param):
    schema = param.get_schema()
    luminaire_info_props = schema["properties"]["luminaire_info"]["properties"]
    for _, key, _, _, _ in _FIELD_SPECS:
        assert key in luminaire_info_props


def test_get_schema_integer_field_type(param):
    schema = param.get_schema()
    assert schema["properties"]["luminaire_info"]["properties"]["cri"]["type"] == "integer"


def test_get_schema_string_field_type(param):
    schema = param.get_schema()
    assert schema["properties"]["luminaire_info"]["properties"]["luminaire_color"]["type"] == "string"


def test_get_schema_fields_are_read_only(param):
    schema = param.get_schema()
    for _, key, _, _, _ in _FIELD_SPECS:
        assert schema["properties"]["luminaire_info"]["properties"][key]["options"]["wb"]["read_only"] is True


def test_get_schema_has_ru_translations(param):
    schema = param.get_schema()
    ru = schema["translations"]["ru"]
    assert "CRI" in ru
    assert ru["CRI"] == "CRI"
    assert "Luminaire information" in ru
    assert ru["Luminaire information"] == "Информация о светильнике"


# ---------------------------------------------------------------------------
# write() – must be a no-op (read-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_returns_empty(param, mock_driver):
    result = await param.write(mock_driver, short_address=1, value={"luminaire_info": {"cri": 99}})
    assert result == {}


# ---------------------------------------------------------------------------
# Type50Parameters integration
# ---------------------------------------------------------------------------


def test_type50_parameters_has_memory_bank_param():
    tp = Type50Parameters()
    assert len(tp._parameters) == 1
    assert isinstance(tp._parameters[0], Type50MemoryBankParam)
