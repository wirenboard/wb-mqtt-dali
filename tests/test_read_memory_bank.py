"""Selective memory-bank reads, driven against python-dali fake gear."""

import logging
import unittest
from typing import Optional

from dali.address import GearShort
from dali.command import Command
from dali.frame import BackwardFrameError
from dali.gear.general import DTR0, ReadMemoryLocation
from dali.memory import info, oem
from dali.tests import fakes

from wb.mqtt_dali.common_dali_device import GeneralMemoryParams, read_memory_bank
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.wbdali import FramePriority

_LOGGER = logging.getLogger("test_read_memory_bank")
_ADDRESS = GearShort(0)

_MANUFACTURER_GTIN = 0x0102030405
_LUMINAIRE_ID = 0x1112131415161718
_LEGACY_IDENTIFICATION_NUMBER = 0x0A0B0C0D


class _OemBank(fakes.FakeBank1):
    initial_contents = [
        0x77,
        None,
        0xFF,
        *_MANUFACTURER_GTIN.to_bytes(6, "big"),
        *_LUMINAIRE_ID.to_bytes(8, "big"),
        0x00,
        0x03,
        24,  # YearOfManufacture
        7,  # WeekOfManufacture
        0x00,
        0x28,  # InputPowerNominal
    ]


class _ShortOemBank(_OemBank):
    initial_contents = [0x0C, *_OemBank.initial_contents[1:]]


class _LegacyBank0(fakes.FakeMemoryBank):
    bank = info.BANK_0_legacy
    initial_contents = [
        0x0E,
        None,
        0x01,
        *(1234567654321).to_bytes(6, "big"),
        0x01,
        0x00,
        *_LEGACY_IDENTIFICATION_NUMBER.to_bytes(4, "big"),
        *([None] * 7),
        0x01,  # QueryVersionNumber answer: DALI version 1
    ]


class _NoProducts:  # pylint: disable=too-few-public-methods
    def get_info_by_gtin(self, gtin):
        del gtin


class _FakeBusDriver:
    """Runs sequences against one fake gear, recording every frame and each read location."""

    def __init__(
        self,
        gear: fakes.Gear,
        framing_errors: Optional[dict[int, int]] = None,
    ) -> None:
        self.gear = gear
        self.bus = fakes.Bus([gear])
        self.framing_errors = dict(framing_errors or {})
        self.commands: list[Command] = []
        self.read_locations: list[int] = []

    async def send(self, cmd, source=None, priority=None, logger=None):
        del source, priority, logger
        return self.send_frame(cmd)

    async def run_sequence(self, seq, priority=None, logger=None):
        del priority, logger
        response = None
        while True:
            try:
                cmd = seq.send(response)
            except StopIteration as result:
                return result.value
            if isinstance(cmd, list):
                response = [self.send_frame(c) for c in cmd]
            else:
                response = self.send_frame(cmd)

    def send_frame(self, cmd):
        self.commands.append(cmd)
        if not isinstance(cmd, ReadMemoryLocation):
            return self.bus.send(cmd)
        location = self.gear.dtr0
        self.read_locations.append(location)
        response = self.bus.send(cmd)
        if self.framing_errors.get(location, 0) > 0:
            self.framing_errors[location] -= 1
            return cmd.response(BackwardFrameError(0))
        return response

    def dtr0_values(self) -> list[int]:
        return [c.param for c in self.commands if isinstance(c, DTR0)]


def _driver(banks: tuple, framing_errors: Optional[dict[int, int]] = None) -> _FakeBusDriver:
    return _FakeBusDriver(fakes.Gear(shortaddr=0, memory_banks=banks), framing_errors)


async def _read(driver: _FakeBusDriver, bank, values) -> dict:
    return await driver.run_sequence(
        read_memory_bank(bank, _ADDRESS, DaliCommandsCompatibilityLayer(), values),
        FramePriority.CONFIGURATION,
        _LOGGER,
    )


class ReadMemoryBankSelectedValuesTest(unittest.IsolatedAsyncioTestCase):
    async def test_contiguous_values_read_with_one_dtr0(self):
        """Two adjacent bank 1 values: after the LastAddress probe one DTR0, then only their locations."""
        driver = _driver((_OemBank,))

        result = await _read(driver, oem.BANK_1, [oem.ManufacturerGTIN, oem.LuminaireID])

        assert result == {oem.ManufacturerGTIN: _MANUFACTURER_GTIN, oem.LuminaireID: _LUMINAIRE_ID}
        assert driver.dtr0_values() == [0x00, 0x03]
        assert driver.read_locations == [0x00, *range(0x03, 0x11)]

    async def test_locations_between_values_are_read_through(self):
        """Values ten locations apart: one DTR0, the locations between them are read too."""
        driver = _driver((_OemBank,))

        result = await _read(driver, oem.BANK_1, [oem.ManufacturerGTIN, oem.YearOfManufacture])

        assert result == {oem.ManufacturerGTIN: _MANUFACTURER_GTIN, oem.YearOfManufacture: 24}
        assert driver.dtr0_values() == [0x00, 0x03]
        assert driver.read_locations == [0x00, *range(0x03, 0x14)]

    async def test_value_beyond_last_address_is_absent(self):
        """Bank ends at 0x0c inside LuminaireID: it is absent, nothing past 0x0c is read."""
        driver = _driver((_ShortOemBank,))

        result = await _read(
            driver, oem.BANK_1, [oem.ManufacturerGTIN, oem.LuminaireID, oem.YearOfManufacture]
        )

        assert result == {oem.ManufacturerGTIN: _MANUFACTURER_GTIN}
        assert driver.read_locations == [0x00, *range(0x03, 0x0D)]

    async def test_read_error_retries_from_first_unread_location(self):
        """A framing error at 0x06 once: the run is re-read from 0x06 and the values are complete."""
        driver = _driver((_OemBank,), {0x06: 1})

        result = await _read(driver, oem.BANK_1, [oem.ManufacturerGTIN, oem.LuminaireID])

        assert result == {oem.ManufacturerGTIN: _MANUFACTURER_GTIN, oem.LuminaireID: _LUMINAIRE_ID}
        assert driver.dtr0_values() == [0x00, 0x03, 0x06]
        assert driver.read_locations == [0x00, *range(0x03, 0x11), *range(0x06, 0x11)]

    async def test_read_error_three_times_raises(self):
        """A framing error at 0x06 on every attempt: three attempts, then RuntimeError naming the location."""
        driver = _driver((_OemBank,), {0x06: 3})

        with self.assertRaisesRegex(RuntimeError, "stopped at location 6"):
            await _read(driver, oem.BANK_1, [oem.ManufacturerGTIN, oem.LuminaireID])

        assert driver.dtr0_values() == [0x00, 0x03, 0x06, 0x06]


class GeneralMemoryParamsReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_only_locations_of_its_fields(self):
        """Banks 0 and 1 populated: every field is returned, read from 0x03..0x14 and 0x03..0x10 only,
        in fewer frames than whole-bank reads."""
        driver = _driver((fakes.FakeBank0, _OemBank))
        whole_bank_driver = _driver((fakes.FakeBank0, _OemBank))
        await _read(whole_bank_driver, info.BANK_0, None)
        await _read(whole_bank_driver, oem.BANK_1, None)

        result = await GeneralMemoryParams(DaliCommandsCompatibilityLayer(), _NoProducts()).read(
            driver, _ADDRESS, _LOGGER
        )

        assert result == {
            "gtin": 1234567654321,
            "firmware_version": "1.0",
            "identification_number": "1",
            "hardware_version": "2.1",
            "oem_gtin": _MANUFACTURER_GTIN,
            "oem_identification_number": str(_LUMINAIRE_ID),
        }
        assert driver.read_locations == [0x00, *range(0x03, 0x15), 0x00, *range(0x03, 0x11)]
        assert len(driver.commands) < len(whole_bank_driver.commands)

    async def test_legacy_bank_0_reads_identification_number_at_legacy_addresses(self):
        """DALI version 1 gear: IdentificationNumber is read from the legacy 0x0b..0x0e locations."""
        driver = _driver((_LegacyBank0,))

        result = await GeneralMemoryParams(DaliCommandsCompatibilityLayer(), _NoProducts()).read(
            driver, _ADDRESS, _LOGGER
        )

        assert result == {"identification_number": str(_LEGACY_IDENTIFICATION_NUMBER)}
        assert driver.read_locations == [0x00, *range(0x0B, 0x0F), 0x00, 0x00, 0x00]
