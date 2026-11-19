"""What repeated initialization of a silent device logs, and the device loggers it goes to."""

import logging
import unittest
from typing import List, Set
from unittest.mock import MagicMock

from wb.mqtt_dali.application_controller import try_initialize_device
from wb.mqtt_dali.dali2_device import Dali2Device
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.device_init_scheduler import DeviceInitScheduler
from wb.mqtt_dali.wbdali_error_response import NoPowerOnBus

from ._bus_doubles import BUS_LOGGER, BUS_UID, FakeWBDALIDriver, make_device
from ._virtual_time import VirtualTimeTestCase
from .test_device_init_retry import _make_publisher

INIT_CYCLES = 3


def _above_debug(records: List[logging.LogRecord]) -> List[tuple]:
    return [(r.name, r.levelno, r.getMessage()) for r in records if r.levelno > logging.DEBUG]


class _InitTestCase(VirtualTimeTestCase):
    def setUp(self) -> None:
        self.driver = FakeWBDALIDriver()
        self.scheduler = DeviceInitScheduler()

    async def _try_initialize(self, device) -> bool:
        return await try_initialize_device(
            device, self.driver, _make_publisher(), self.scheduler, MagicMock(), BUS_LOGGER, 0.0
        )


class TestInitRetryWithoutPower(_InitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.powered = False
        self.exchange_loggers: Set[str] = set()
        send = self.driver.send

        async def send_unless_unpowered(cmd, source=None, priority=None, logger=None):
            if self.powered:
                return await send(cmd, source, priority, logger)
            self.exchange_loggers.add(logger.name)
            response = NoPowerOnBus()
            self.driver.report(cmd, response, source)
            return response

        self.driver.send = send_unless_unpowered
        self.dali_device = make_device(DaliDevice, 1)
        self.dali2_device = make_device(Dali2Device, 2)

    async def test_bus_without_power_logs_one_failure_per_device(self):
        """No power, a DALI and a DALI-2 device retried several times: one initialization failure
        per device in its own logger, which is also the one each exchange goes out with."""
        with self.assertLogs(BUS_UID, logging.DEBUG) as logs:
            for _ in range(INIT_CYCLES):
                for device in (self.dali_device, self.dali2_device):
                    self.assertFalse(await self._try_initialize(device))

        above_debug = _above_debug(logs.records)
        self.assertEqual(
            [(name, level) for name, level, _ in above_debug],
            [(f"{BUS_UID}.dali_1", logging.WARNING), (f"{BUS_UID}.dali2_2", logging.WARNING)],
        )
        for _, _, message in above_debug:
            self.assertTrue(message.startswith("Failed to initialize device "))
            self.assertIn("no power on bus", message.lower())
        self.assertEqual(self.exchange_loggers, {f"{BUS_UID}.dali_1", f"{BUS_UID}.dali2_2"})

    async def test_power_back_logs_one_success_per_device(self):
        """After failed retries without power the power comes back and the devices answer: one INFO
        per device about its initialization."""
        for _ in range(INIT_CYCLES):
            for device in (self.dali_device, self.dali2_device):
                await self._try_initialize(device)

        self.powered = True
        with self.assertLogs(BUS_UID, logging.DEBUG) as logs:
            for device in (self.dali_device, self.dali2_device):
                self.assertTrue(await self._try_initialize(device))

        self.assertEqual(
            _above_debug(logs.records),
            [
                (
                    f"{BUS_UID}.dali_1",
                    logging.INFO,
                    f"Device {self.dali_device.name} initialized successfully after retry",
                ),
                (
                    f"{BUS_UID}.dali2_2",
                    logging.INFO,
                    f"Device {self.dali2_device.name} initialized successfully after retry",
                ),
            ],
        )

    async def test_exchange_of_a_device_with_a_custom_mqtt_id_goes_to_its_logger(self):
        """A device with a custom mqtt_id fails to initialize: its exchange goes out with, and its
        failure is logged under, `<bus>.<mqtt_id>`."""
        device = make_device(DaliDevice, 3, "hall_lamp")

        with self.assertLogs(BUS_UID, logging.DEBUG) as logs:
            self.assertFalse(await self._try_initialize(device))

        self.assertEqual(self.exchange_loggers, {f"{BUS_UID}.hall_lamp"})
        failures = [
            name for name, _, message in _above_debug(logs.records) if "Failed to initialize" in message
        ]
        self.assertEqual(failures, [f"{BUS_UID}.hall_lamp"])


class TestInitRetryOfRemovedDevice(_InitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.driver.empty_bus = True

    async def test_device_removed_from_the_bus_logs_one_failure(self):
        """Power on, the device is gone: the first attempt logs one failure in its logger, the
        following attempts nothing above DEBUG."""
        device = make_device(DaliDevice, 1)

        with self.assertLogs(BUS_UID, logging.DEBUG) as first:
            self.assertFalse(await self._try_initialize(device))
        with self.assertNoLogs(BUS_UID, logging.INFO):
            for _ in range(INIT_CYCLES - 1):
                self.assertFalse(await self._try_initialize(device))

        above_debug = _above_debug(first.records)
        self.assertEqual(
            [(name, level) for name, level, _ in above_debug], [(f"{BUS_UID}.dali_1", logging.WARNING)]
        )
        self.assertTrue(above_debug[0][2].startswith(f"Failed to initialize device {device.name}: "))


class TestDeviceLoggerNames(unittest.TestCase):
    def test_device_logger_is_named_after_the_address_or_the_custom_mqtt_id(self):
        """Default ids name the logger by kind and address, a custom mqtt_id by itself; changes follow."""
        dali = make_device(DaliDevice, 5)
        dali2 = make_device(Dali2Device, 5)
        custom = make_device(DaliDevice, 7, "hall_lamp")

        self.assertEqual(
            [dali.logger.name, dali2.logger.name, custom.logger.name],
            [f"{BUS_UID}.dali_5", f"{BUS_UID}.dali2_5", f"{BUS_UID}.hall_lamp"],
        )

        dali.address.short = 6
        custom.mqtt_id = "corridor_lamp"
        self.assertEqual(
            [dali.logger.name, custom.logger.name], [f"{BUS_UID}.dali_6", f"{BUS_UID}.corridor_lamp"]
        )
