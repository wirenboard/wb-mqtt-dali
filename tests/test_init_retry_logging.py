"""What repeated initialization of a silent device logs, and the device loggers it goes to."""

import asyncio
import contextlib
import json
import logging
import unittest
from typing import List, Optional
from unittest.mock import MagicMock, mock_open, patch

import aiomqtt

from wb.mqtt_dali.application_controller import try_initialize_device
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress
from wb.mqtt_dali.dali2_device import Dali2Device
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.device_init_scheduler import DeviceInitScheduler
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver

from ._broker_link_helpers import RecordingClient
from ._on_off_helpers import BusScript, ReplyStatus, ScriptedGatewayClient
from .test_device_init_retry import _make_publisher

GATEWAY_ID = "gw1"
BUS_INDEX = 1
BUS_UID = f"{GATEWAY_ID}_bus_{BUS_INDEX}"
BUS_LOGGER = logging.getLogger(BUS_UID)
LOAD_TOPIC_PREFIX = "/rpc/v1/wb-mqtt-serial/port/Load/"
MODBUS_WRITE_REGISTERS = 16
INIT_CYCLES = 3


class _PowerSwitchedGatewayClient(ScriptedGatewayClient):
    """Scripted gateway whose bus power can be cut: then every queued frame fails with no power."""

    def __init__(self, script: Optional[BusScript] = None) -> None:
        super().__init__(script or BusScript(), GATEWAY_ID, BUS_INDEX)
        self.bus_powered = True
        self._queue_config = WBDALIConfig(device_name=GATEWAY_ID, bus=BUS_INDEX)

    async def publish(
        self, topic: str, payload: Optional[str] = None, qos: int = 0, retain: bool = False
    ) -> None:
        if self.bus_powered or not topic.startswith(LOAD_TOPIC_PREFIX) or payload is None:
            await super().publish(topic, payload, qos, retain)
            return
        await RecordingClient.publish(self, topic, payload, qos, retain)
        params = json.loads(payload)["params"]
        if params["function"] != MODBUS_WRITE_REGISTERS:
            return
        start_slot = (params["address"] - self._queue_config.queue_start_modbus_address) // 2
        for slot in range(start_slot, start_slot + params["count"] // 2):
            reply_topic = f"/devices/{GATEWAY_ID}/controls/bus_{BUS_INDEX}_bulk_send_reply_{slot}"
            reply = str(ReplyStatus.NO_POWER_ON_BUS << 8)
            self.messages.deliver(aiomqtt.Message(reply_topic, reply, 0, False, 0, None))


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _make_device(device_class, short: int, mqtt_id: Optional[str] = None):
    # The common settings schema is read from the installed package; tests get a stub.
    with patch("wb.mqtt_dali.common_dali_device.open", mock_open(read_data='{"title": "test"}'), create=True):
        device = device_class(DaliDeviceAddress(short=short, random=0), BUS_UID, MagicMock(), mqtt_id)
    device.set_logger(BUS_LOGGER)
    return device


def _above_debug(records: List[logging.LogRecord]) -> List[tuple]:
    return [(r.name, r.levelno, r.getMessage()) for r in records if r.levelno > logging.DEBUG]


def _debug_names(records: List[logging.LogRecord], text: str) -> set:
    return {r.name for r in records if r.levelno == logging.DEBUG and text in r.getMessage()}


class _GatewayTestCase(unittest.IsolatedAsyncioTestCase):
    script: Optional[BusScript] = None

    def setUp(self) -> None:
        self.client = _PowerSwitchedGatewayClient(self.script)
        self.dispatcher = MQTTDispatcher(self.client)
        self.driver = WBDALIDriver(WBDALIConfig(GATEWAY_ID, BUS_INDEX), self.dispatcher, BUS_LOGGER)
        self.scheduler = DeviceInitScheduler()

    async def asyncSetUp(self) -> None:
        dispatch_task = asyncio.create_task(self.dispatcher.run())
        self.addAsyncCleanup(_cancel, dispatch_task)
        await self.driver.initialize()
        self.addAsyncCleanup(self.driver.deinitialize)

    async def _try_initialize(self, device) -> bool:
        return await try_initialize_device(
            device, self.driver, _make_publisher(), self.scheduler, MagicMock(), BUS_LOGGER, 0.0
        )


class TestInitRetryWithoutPower(_GatewayTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.bus_powered = False
        self.dali_device = _make_device(DaliDevice, 1)
        self.dali2_device = _make_device(Dali2Device, 2)

    async def test_bus_without_power_logs_one_power_warning_and_one_failure_per_device(self):
        """No power, a DALI and a DALI-2 device retried several times: one power WARNING in the
        bus logger and one initialization failure per device in its own logger."""
        with self.assertLogs(BUS_UID, logging.DEBUG) as logs:
            for _ in range(INIT_CYCLES):
                for device in (self.dali_device, self.dali2_device):
                    self.assertFalse(await self._try_initialize(device))

        above_debug = _above_debug(logs.records)
        self.assertEqual(above_debug[0], (BUS_UID, logging.WARNING, "No power on DALI bus"))
        self.assertEqual(len(above_debug), 3)
        self.assertEqual(
            [(name, level) for name, level, _ in above_debug[1:]],
            [(f"{BUS_UID}.dali_1", logging.WARNING), (f"{BUS_UID}.dali2_2", logging.WARNING)],
        )
        for _, _, message in above_debug[1:]:
            self.assertTrue(message.startswith("Failed to initialize device "))
            self.assertIn("no power on bus", message.lower())
        self.assertEqual(_debug_names(logs.records, "status 4"), {f"{BUS_UID}.dali_1", f"{BUS_UID}.dali2_2"})

    async def test_power_back_logs_its_return_and_one_success_per_device(self):
        """After failed retries without power the power comes back and the devices answer: an INFO
        about the power and one INFO per device about its initialization."""
        for _ in range(INIT_CYCLES):
            for device in (self.dali_device, self.dali2_device):
                await self._try_initialize(device)

        self.client.bus_powered = True
        with self.assertLogs(BUS_UID, logging.DEBUG) as logs:
            for device in (self.dali_device, self.dali2_device):
                self.assertTrue(await self._try_initialize(device))

        self.assertEqual(
            _above_debug(logs.records),
            [
                (BUS_UID, logging.INFO, "DALI bus power restored"),
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
        """A device with a custom mqtt_id fails to initialize: its exchange and its failure are
        logged under `<bus>.<mqtt_id>`."""
        device = _make_device(DaliDevice, 3, "hall_lamp")

        with self.assertLogs(BUS_UID, logging.DEBUG) as logs:
            self.assertFalse(await self._try_initialize(device))

        self.assertEqual(_debug_names(logs.records, "status 4"), {f"{BUS_UID}.hall_lamp"})
        failures = [
            name for name, _, message in _above_debug(logs.records) if "Failed to initialize" in message
        ]
        self.assertEqual(failures, [f"{BUS_UID}.hall_lamp"])


class TestInitRetryOfRemovedDevice(_GatewayTestCase):
    script = BusScript(present_shorts=frozenset())

    async def test_device_removed_from_the_bus_logs_one_failure(self):
        """Power on, the device is gone: the first attempt logs one failure in its logger, the
        following attempts nothing above DEBUG."""
        device = _make_device(DaliDevice, 1)

        with self.assertLogs(BUS_UID, logging.DEBUG) as first:
            self.assertFalse(await self._try_initialize(device))
        with self.assertLogs(BUS_UID, logging.DEBUG) as retries:
            for _ in range(INIT_CYCLES - 1):
                self.assertFalse(await self._try_initialize(device))

        above_debug = _above_debug(first.records)
        self.assertEqual(
            [(name, level) for name, level, _ in above_debug], [(f"{BUS_UID}.dali_1", logging.WARNING)]
        )
        self.assertTrue(above_debug[0][2].startswith(f"Failed to initialize device {device.name}: "))
        self.assertEqual(_above_debug(retries.records), [])


class TestDeviceLoggerNames(unittest.TestCase):
    def test_device_logger_is_named_after_the_address_or_the_custom_mqtt_id(self):
        """Default ids name the logger by kind and address, a custom mqtt_id by itself; changes follow."""
        dali = _make_device(DaliDevice, 5)
        dali2 = _make_device(Dali2Device, 5)
        custom = _make_device(DaliDevice, 7, "hall_lamp")

        self.assertEqual(
            [dali.logger.name, dali2.logger.name, custom.logger.name],
            [f"{BUS_UID}.dali_5", f"{BUS_UID}.dali2_5", f"{BUS_UID}.hall_lamp"],
        )

        dali.address.short = 6
        custom.mqtt_id = "corridor_lamp"
        self.assertEqual(
            [dali.logger.name, custom.logger.name], [f"{BUS_UID}.dali_6", f"{BUS_UID}.corridor_lamp"]
        )
