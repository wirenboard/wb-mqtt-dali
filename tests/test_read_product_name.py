from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.address import GearShort

from wb.mqtt_dali.common_dali_device import read_product_name
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer

# pylint: disable=protected-access


@pytest.mark.asyncio
async def test_read_product_name_returns_string_when_gtin_is_known():
    driver = AsyncMock()
    gtin_db = MagicMock()
    gtin_db.get_info_by_gtin.return_value = {
        "brand_name": "Acme",
        "product_name": "SuperLamp 100",
    }

    with patch(
        "wb.mqtt_dali.common_dali_device.read_gtin_fast",
        new=AsyncMock(return_value=8721103129109),
    ):
        name = await read_product_name(driver, GearShort(5), DaliCommandsCompatibilityLayer(), gtin_db)

    assert name == "SuperLamp 100"
    gtin_db.get_info_by_gtin.assert_called_once_with(8721103129109)


@pytest.mark.asyncio
async def test_read_product_name_uses_oem_gtin_when_primary_missing():
    # ``read_gtin_fast`` hides the bank-0 vs bank-1 decision; ``read_product_name``
    # only sees the final GTIN integer. The fallback to bank 1 is verified in
    # test_read_gtin_fast.py; here we confirm the caller handles any GTIN value.
    driver = AsyncMock()
    gtin_db = MagicMock()
    gtin_db.get_info_by_gtin.return_value = {"product_name": "OEM Lamp"}

    with patch(
        "wb.mqtt_dali.common_dali_device.read_gtin_fast",
        new=AsyncMock(return_value=42),
    ):
        name = await read_product_name(driver, GearShort(1), DaliCommandsCompatibilityLayer(), gtin_db)

    assert name == "OEM Lamp"
    gtin_db.get_info_by_gtin.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_read_product_name_returns_none_when_no_gtin():
    driver = AsyncMock()
    gtin_db = MagicMock()

    with patch(
        "wb.mqtt_dali.common_dali_device.read_gtin_fast",
        new=AsyncMock(return_value=None),
    ):
        name = await read_product_name(driver, GearShort(2), DaliCommandsCompatibilityLayer(), gtin_db)

    assert name is None
    gtin_db.get_info_by_gtin.assert_not_called()


@pytest.mark.asyncio
async def test_read_product_name_returns_none_when_gtin_unknown():
    driver = AsyncMock()
    gtin_db = MagicMock()
    gtin_db.get_info_by_gtin.return_value = None

    with patch(
        "wb.mqtt_dali.common_dali_device.read_gtin_fast",
        new=AsyncMock(return_value=999),
    ):
        name = await read_product_name(driver, GearShort(3), DaliCommandsCompatibilityLayer(), gtin_db)

    assert name is None
    gtin_db.get_info_by_gtin.assert_called_once_with(999)


@pytest.mark.asyncio
async def test_read_product_name_returns_none_when_memory_read_raises():
    driver = AsyncMock()
    gtin_db = MagicMock()
    logger = MagicMock()

    with patch(
        "wb.mqtt_dali.common_dali_device.read_gtin_fast",
        new=AsyncMock(side_effect=RuntimeError("bus timeout")),
    ):
        name = await read_product_name(
            driver, GearShort(4), DaliCommandsCompatibilityLayer(), gtin_db, logger
        )

    assert name is None
    gtin_db.get_info_by_gtin.assert_not_called()
    # Warning should be logged on memory read failure for observability
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_read_product_name_returns_none_when_product_name_missing_from_entry():
    driver = AsyncMock()
    gtin_db = MagicMock()
    # The database entry has no product_name field (e.g., malformed row)
    gtin_db.get_info_by_gtin.return_value = {"brand_name": "X"}

    with patch(
        "wb.mqtt_dali.common_dali_device.read_gtin_fast",
        new=AsyncMock(return_value=7),
    ):
        name = await read_product_name(driver, GearShort(6), DaliCommandsCompatibilityLayer(), gtin_db)

    assert name is None
