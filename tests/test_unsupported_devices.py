"""Gear that answers QUERY CONTROL GEAR but not QUERY DEVICE TYPE, like a Helvar 311 sensor,
on a running bus over the fake driver."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Iterable
from unittest.mock import MagicMock

from dali.address import GearShort
from dali.command import Command, Response
from dali.gear.general import QueryControlGearPresent, QueryDeviceType

from wb.mqtt_dali.application_controller import ApplicationController
from wb.mqtt_dali.control_ids import ACTUAL_LEVEL
from wb.mqtt_dali.device_init_scheduler import (
    INIT_RETRY_MAX_DELAY,
    UNSUPPORTED_DEVICE_RETRY_DELAY_S,
)
from wb.mqtt_dali.gateway import Gateway, WbDaliGateway

from ._bus_doubles import (
    BUS_UID,
    INIT_S,
    SETTLE_S,
    FakeWBDALIDriver,
    control_error,
    is_published,
    payloads,
    running_bus,
)
from ._common_schema import load_common_schema
from ._virtual_time import VirtualTimeTestCase

LIGHT = 5
SENSOR = 6
LIGHT_ID = f"{BUS_UID}_{LIGHT}"
SENSOR_ID = f"{BUS_UID}_{SENSOR}"
SENSOR_LOGGER = f"{BUS_UID}.dali_{SENSOR}"
SENSOR_META = f"/devices/{SENSOR_ID}/meta"
QUERY_ATTEMPTS = 3
# Past the backoff ramp: from here on a silent device is retried once per INIT_RETRY_MAX_DELAY.
BACKOFF_RAMP_S = 200.0


class _Helvar311Driver(FakeWBDALIDriver):
    """`unsupported` gear answers QUERY CONTROL GEAR only and `absent` gear nothing; the first
    `lost_presence_answers` QUERY CONTROL GEAR answers are lost."""

    def __init__(
        self, unsupported: Iterable[int] = (), absent: Iterable[int] = (), lost_presence_answers: int = 0
    ) -> None:
        super().__init__()
        self.unsupported = set(unsupported)
        self.absent = set(absent)
        self.lost_presence_answers = lost_presence_answers

    async def send(self, cmd, source=None, priority=None, logger=None):
        if not self._is_silent(cmd):
            return await super().send(cmd, source, priority, logger)
        self.sent.append(cmd)
        response = cmd.response(None) if cmd.response is not None else Response(None)
        self.report(cmd, response, source)
        return response

    # --- Private ---

    def _is_silent(self, cmd: Command) -> bool:
        destination = getattr(cmd, "destination", None)
        if not isinstance(destination, GearShort):
            return False
        if destination.address in self.absent:
            return True
        if destination.address not in self.unsupported:
            return False
        if not isinstance(cmd, QueryControlGearPresent):
            return True
        if self.lost_presence_answers > 0:
            self.lost_presence_answers -= 1
            return True
        return False


def _bus_data(*shorts: int) -> dict:
    return {"devices": [{"short": short, "random": 0} for short in shorts]}


def _frames_to(driver: FakeWBDALIDriver, short: int, command_class: type = Command) -> int:
    return sum(
        1
        for command in driver.sent
        if isinstance(command, command_class) and getattr(command, "destination", None) == GearShort(short)
    )


def _init_attempts(driver: FakeWBDALIDriver, short: int) -> int:
    return _frames_to(driver, short, QueryDeviceType) // QUERY_ATTEMPTS


def _make_gateway(bus: ApplicationController, config_path: str) -> Gateway:
    gateway = Gateway({}, MagicMock(), config_path, MagicMock(), load_common_schema(), command_registry={})
    gateway.wb_dali_gateways = [WbDaliGateway(uid="gw1", buses=[bus])]
    return gateway


def _warnings(records: list[logging.LogRecord]) -> list[tuple[str, str]]:
    return [(r.name, r.getMessage()) for r in records if r.levelno == logging.WARNING]


class UnsupportedDeviceTest(VirtualTimeTestCase):
    async def test_unsupported_device_is_not_published_and_warns(self):
        """A light and a sensor silent on QUERY DEVICE TYPE but answering QUERY CONTROL GEAR: the light
        is published, the sensor is not; a warning that the sensor does not support DALI."""
        async with running_bus(_bus_data(LIGHT, SENSOR), _Helvar311Driver(unsupported={SENSOR})) as (
            _bus,
            client,
        ):
            with self.assertLogs(BUS_UID, logging.WARNING) as logs:
                await asyncio.sleep(INIT_S)

            self.assertTrue(is_published(client, f"/devices/{LIGHT_ID}/meta"))
            self.assertFalse(is_published(client, SENSOR_META))
            self.assertIn(
                (
                    SENSOR_LOGGER,
                    "Device DALI 6 answers QUERY CONTROL GEAR but not QUERY DEVICE TYPE: "
                    "looks like it does not support DALI commands",
                ),
                _warnings(logs.records),
            )

    async def test_unsupported_device_is_retried_once_an_hour_silently(self):
        """After start the sensor gets no frames for an hour, then exactly one initialization
        attempt that logs nothing above DEBUG."""
        driver = _Helvar311Driver(unsupported={SENSOR})
        async with running_bus(_bus_data(SENSOR), driver) as (_bus, client):
            await asyncio.sleep(INIT_S)
            driver.sent.clear()

            await asyncio.sleep(UNSUPPORTED_DEVICE_RETRY_DELAY_S - 2 * INIT_S)
            self.assertEqual(_frames_to(driver, SENSOR), 0)

            with self.assertNoLogs(BUS_UID, logging.INFO):
                await asyncio.sleep(2 * INIT_S)

            self.assertEqual(_init_attempts(driver, SENSOR), 1)
            self.assertEqual(_frames_to(driver, SENSOR, QueryControlGearPresent), 1)
            self.assertFalse(is_published(client, SENSOR_META))

    async def test_unsupported_device_that_starts_answering_is_published(self):
        """The sensor starts answering QUERY DEVICE TYPE: on the next hourly retry it initializes,
        is published with its controls without a read error, and one INFO says so."""
        driver = _Helvar311Driver(unsupported={SENSOR})
        async with running_bus(_bus_data(SENSOR), driver) as (bus, client):
            await asyncio.sleep(INIT_S)
            driver.unsupported.discard(SENSOR)

            with self.assertLogs(BUS_UID, logging.INFO) as logs:
                await asyncio.sleep(UNSUPPORTED_DEVICE_RETRY_DELAY_S + INIT_S)

            self.assertTrue(bus.dali_devices[0].is_initialized)
            self.assertTrue(is_published(client, SENSOR_META))
            self.assertTrue(is_published(client, f"/devices/{SENSOR_ID}/controls/{ACTUAL_LEVEL}/meta"))
            self.assertIsNone(control_error(client, SENSOR_ID, ACTUAL_LEVEL))
            self.assertIn(
                (SENSOR_LOGGER, "Device DALI 6 initialized successfully after retry"),
                [(r.name, r.getMessage()) for r in logs.records if r.levelno == logging.INFO],
            )

    async def test_published_device_found_unsupported_is_removed(self):
        """A silent device is published with its controls in error; on a retry it answers QUERY
        CONTROL GEAR only: it is removed from MQTT."""
        driver = _Helvar311Driver(absent={SENSOR})
        async with running_bus(_bus_data(SENSOR), driver) as (_bus, client):
            await asyncio.sleep(INIT_S)
            self.assertEqual(control_error(client, SENSOR_ID, ACTUAL_LEVEL), "r")

            driver.absent.discard(SENSOR)
            driver.unsupported.add(SENSOR)
            await asyncio.sleep(INIT_RETRY_MAX_DELAY + INIT_S)

            self.assertIsNone(payloads(client, SENSOR_META)[-1])

    async def test_silent_device_keeps_todays_behaviour(self):
        """A device silent on QUERY CONTROL GEAR too: published with its controls in error, a "does not
        respond" warning, and once the backoff is over one retry a minute, each asking it three times."""
        driver = _Helvar311Driver(absent={SENSOR})
        async with running_bus(_bus_data(SENSOR), driver) as (_bus, client):
            with self.assertLogs(BUS_UID, logging.WARNING) as logs:
                await asyncio.sleep(INIT_S)

            self.assertTrue(is_published(client, SENSOR_META))
            self.assertEqual(control_error(client, SENSOR_ID, ACTUAL_LEVEL), "r")
            self.assertEqual(_frames_to(driver, SENSOR, QueryControlGearPresent), QUERY_ATTEMPTS)
            self.assertEqual(
                _warnings(logs.records),
                [
                    (
                        SENSOR_LOGGER,
                        "Failed to initialize device DALI 6: "
                        "does not respond: check power and bus connection",
                    )
                ],
            )

            await asyncio.sleep(BACKOFF_RAMP_S)
            driver.sent.clear()
            await asyncio.sleep(INIT_RETRY_MAX_DELAY + 1)

            self.assertEqual(_init_attempts(driver, SENSOR), 1)

    async def test_presence_answered_on_second_attempt_is_unsupported(self):
        """The first QUERY CONTROL GEAR goes unanswered, the second gets YES: the device does not
        support DALI and the third is not sent."""
        driver = _Helvar311Driver(unsupported={SENSOR}, lost_presence_answers=1)
        async with running_bus(_bus_data(SENSOR), driver) as (bus, _client):
            await asyncio.sleep(INIT_S)

            self.assertEqual(_frames_to(driver, SENSOR, QueryControlGearPresent), 2)
            self.assertTrue(bus.dali_devices[0].is_helvar)

    async def test_successful_initialization_sends_no_presence_query(self):
        """A light that answers QUERY DEVICE TYPE initializes without a QUERY CONTROL GEAR."""
        driver = _Helvar311Driver()
        async with running_bus(_bus_data(LIGHT), driver) as (bus, _client):
            await asyncio.sleep(INIT_S)

            self.assertTrue(bus.dali_devices[0].is_initialized)
            self.assertEqual(_frames_to(driver, LIGHT, QueryControlGearPresent), 0)


class UnsupportedDeviceEditorTest(VirtualTimeTestCase):
    def setUp(self) -> None:
        config_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, config_dir)
        self.config_path = os.path.join(config_dir, "wb-mqtt-dali.conf")

    async def test_get_device_of_unsupported_device_returns_reduced_form(self):
        """GetDevice of the sensor: its name and addresses, no MQTT id and no device type settings."""
        async with running_bus(_bus_data(SENSOR), _Helvar311Driver(unsupported={SENSOR})) as (bus, client):
            await asyncio.sleep(INIT_S)
            gateway = _make_gateway(bus, self.config_path)

            reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            self.assertEqual(
                reply["config"], {"short_address": SENSOR, "random_address": "0x0", "name": "DALI 6"}
            )
            self.assertNotIn("mqtt_id", reply["schema"].get("properties", {}))
            self.assertFalse(is_published(client, SENSOR_META))

    async def test_set_device_renames_unsupported_device_in_config_only(self):
        """SetDevice with the form GetDevice gave, a new name and another short address: only the
        name is saved, nothing goes to the bus or MQTT."""
        driver = _Helvar311Driver(unsupported={SENSOR})
        async with running_bus(_bus_data(SENSOR), driver) as (bus, client):
            await asyncio.sleep(INIT_S)
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]
            await asyncio.sleep(SETTLE_S)
            driver.sent.clear()

            await gateway.set_device_rpc_handler(
                {"deviceId": device_id, "config": {**form, "name": "Sensor", "short_address": 7}}
            )
            await asyncio.sleep(SETTLE_S)

            with open(self.config_path, encoding="utf-8") as config_file:
                saved = json.load(config_file)["gateways"][0]["buses"][0]["devices"]
            self.assertEqual(saved, [{"short": SENSOR, "random": 0, "name": "Sensor"}])
            self.assertEqual(bus.dali_devices[0].address.short, SENSOR)
            self.assertEqual(driver.sent, [])
            self.assertFalse(is_published(client, SENSOR_META))

    async def test_get_device_of_device_that_starts_answering_returns_full_form(self):
        """The sensor answers QUERY DEVICE TYPE by the time GetDevice is called: the full form comes
        back and the device is published."""
        driver = _Helvar311Driver(unsupported={SENSOR})
        async with running_bus(_bus_data(SENSOR), driver) as (bus, client):
            await asyncio.sleep(INIT_S)
            gateway = _make_gateway(bus, self.config_path)
            driver.unsupported.discard(SENSOR)

            reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            self.assertEqual(reply["config"]["mqtt_id"], SENSOR_ID)
            self.assertEqual(reply["config"]["types"], [])
            self.assertTrue(is_published(client, SENSOR_META))
