"""Helvar Digidim products on a bus over the fake driver: their recognition in commissioning, the
config flag, no traffic to them, their editor form and their FF17 frames in the bus monitor."""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock

from dali.address import DeviceShort, GearShort
from dali.command import Command, Response
from dali.device import general as device_general
from dali.frame import BackwardFrame
from dali.gear import general as gear_general

from wb.mqtt_dali.application_controller import ApplicationController
from wb.mqtt_dali.dali2_device import Dali2Device
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.gateway import Gateway, WbDaliGateway, get_dict_for_device_config
from wb.mqtt_dali.helvar.digidim import ReadE2
from wb.mqtt_dali.helvar_device import HelvarDigidimSensor
from wb.mqtt_dali.wbdali import FramePriority, encode_frame_for_modbus
from wb.mqtt_dali.wbdali_error_response import TransmissionFailed
from wb.mqtt_dali.wbdali_utils import MASK, MAX_COMMAND_RETRIES

from ._bus_doubles import (
    BUS_UID,
    SETTLE_S,
    FakeWBDALIDriver,
    control_error,
    control_values,
    is_published,
    make_bus,
    payloads,
    running_bus,
)
from ._common_schema import load_common_schema
from ._virtual_time import VirtualTimeTestCase

LIGHT = 5
SENSOR = 6
NEW_SENSOR = 9
SENSOR_RANDOM = 0x15DBB4
LIGHT_RANDOM = 0x20AA01
SENSOR_ID = f"{BUS_UID}_{SENSOR}"
SENSOR_META = f"/devices/{SENSOR_ID}/meta"
SENSOR_CONTROLS = f"/devices/{SENSOR_ID}/controls/"
LIGHT_META = f"/devices/{BUS_UID}_{LIGHT}/meta"
DALI2_SENSOR_META = f"/devices/{BUS_UID}_dali2_{SENSOR}/meta"
BUS_MONITOR_TOPIC = f"/wb-dali/{BUS_UID}/bus_monitor"
# Virtual time for commissioning of a few devices to finish.
COMMISSIONING_S = 30.0
# Virtual time for background work: init retries, polling and settings fetch.
BACKGROUND_S = 600.0

GTIN_312 = 5030797003125
GTIN_321 = 5030797003217
GTIN_PHILIPS_302 = 8711500747877
GTIN_UNLISTED_HELVAR = 6418488000017

FF17_BITS = 17
SENSOR_ADDRESS_BYTE = (SENSOR << 1) | 1
MAJOR_VERSION_FRAME = (FF17_BITS, ((SENSOR_ADDRESS_BYTE << 8) | 0xE5) << 1)
IDENTIFICATION_FRAMES = [
    MAJOR_VERSION_FRAME,
    (FF17_BITS, ((SENSOR_ADDRESS_BYTE << 8) | 0xE6) << 1),
    (16, 0xA300),
    (FF17_BITS, ((0xC5 << 8) | 0x00) << 1),
    *[(FF17_BITS, ((SENSOR_ADDRESS_BYTE << 8) | 0x9A) << 1)] * 10,
]
INITIALISE_SENSOR_FRAME = (16, 0xA500 | SENSOR_ADDRESS_BYTE)
TERMINATE_FRAME = (16, 0xA100)
IDENTIFY_S = 5.0
ENABLE_WRITE_E2_FRAME = (FF17_BITS, ((SENSOR_ADDRESS_BYTE << 8) | 0x84) << 1)

# E2 0x00-0xA1 of the live 311 from the bus dump; the rest is 0xFF as there.
E2_311_DUMP = bytes.fromhex(
    "50 30 79 70 03 11 80 15 db b4 01 0a ff 81 ff 06"
    + " ff" * 16
    + " 07 03 06 82 01 00 f9 00 08 04 08 00 00 00 ff ff"
    + " 02 00 ff 1f 00 00 80 04 ff 1f 00 00 80 04 ff 1d 00 00 80 04 ff 1e 00 00"
    + " 84 00 ff fe 00 00 00 04 ff 06 00 00"
    + "".join(f" 81 04 ff {0x10 + scene:02x} 00 00" for scene in range(8))
    + " 00 04 ff 00 00 00 00 04 ff 10 00 00 03 00 ff 1f 00 00 85 00 ff 01 00 00 85 00 ff 02 00 00"
) + b"\xff" * (0x100 - 0xA2)
IDENTITY_E2 = list(range(10))
E2_SETTINGS = [*range(0x0B, 0x0F), *range(0x20, 0x27), *range(0x30, 0x42 + 6), *range(0x54, 0xA2)]
E2_SETTINGS_OLD_311 = [0x0B, *range(0x21, 0x27), *range(0x36, 0x48)]
# A DALI query: no event command of the form describes it.
UNRECOGNISED_SLOT = bytes.fromhex("00 10 ff 90 00 00")
PRESENCE_ON, PRESENCE_OFF = 0x36, 0x42


def _e2(gtin: int, serial: int = 1) -> bytes:
    return bytes.fromhex(f"{gtin:013d}0") + serial.to_bytes(3, "big")


def _image(gtin: int = 5030797003118, **patches: bytes) -> bytearray:
    """The dump image with another GTIN and bytes replaced, keyed `e2_<hex address>`."""
    image = bytearray(E2_311_DUMP)
    image[:10] = _e2(gtin, 1432500)
    for key, data in patches.items():
        start = int(key.removeprefix("e2_"), 16)
        image[start : start + len(data)] = data
    return image


def _write_frame(data: int) -> tuple[int, int]:
    return (FF17_BITS, ((0xC3 << 8) | data) << 1)


def _dtr_frame(e2_address: int) -> tuple[int, int]:
    return (16, 0xA300 | e2_address)


def _event(target: str, command: str, **fields: int) -> dict:
    """An event field of the form; address, group, scene and level default to 0, the fade time to
    «do not change»."""
    return {
        "target": target,
        "address": 0,
        "group": 0,
        "command": command,
        "scene": 0,
        "level": 0,
        "fade_time": -1,
        **fields,
    }


@dataclass
class _Sensor:  # pylint: disable=too-many-instance-attributes
    """A Digidim sensor's FF17 side: the version queries, Read E2 at DTR0 advancing it, Enable Write
    E2 for one Write E2; `silent_opcodes` and Read E2 past `readable_e2` go unanswered, as does the
    first Read E2 of `lost_e2`, which still advances DTR0."""

    e2: bytearray = field(default_factory=_image)
    firmware: tuple[int, int] = (6, 8)
    silent_opcodes: set[int] = field(default_factory=set)
    readable_e2: Optional[int] = None
    lost_e2: Optional[int] = None
    dtr0: int = 0
    write_enabled: bool = False
    read_addresses: list[int] = field(default_factory=list)

    def answer(self, word: int) -> Optional[int]:
        address_byte, data = word >> 8, word & 0xFF
        if address_byte == 0xC3:
            self._write(data)
            return None
        if address_byte != SENSOR_ADDRESS_BYTE or data in self.silent_opcodes:
            return None
        if data == 0x84:
            self.write_enabled = True
            return None
        if data != 0x9A:
            return {0xE5: self.firmware[0], 0xE6: self.firmware[1]}.get(data)
        return self._read()

    # --- Private ---

    def _write(self, data: int) -> None:
        if self.write_enabled:
            self.e2[self.dtr0] = data
            self.dtr0 += 1
        self.write_enabled = False

    def _read(self) -> Optional[int]:
        if self.readable_e2 is not None and self.dtr0 >= self.readable_e2:
            return None
        if self.dtr0 == self.lost_e2:
            self.lost_e2 = None
            self.dtr0 += 1
            return None
        self.read_addresses.append(self.dtr0)
        value = self.e2[self.dtr0]
        self.dtr0 += 1
        return value


@dataclass
class _SearchSpace:
    """Search state of one address space: random address by short address of its devices, random
    addresses of devices whose short address PROGRAM SHORT ADDRESS MASK cleared, unless they keep it."""

    randoms: dict[int, int] = field(default_factory=dict)
    search: list[int] = field(default_factory=lambda: [0, 0, 0])
    initialised: bool = False
    withdrawn: set[int] = field(default_factory=set)
    unaddressed: set[int] = field(default_factory=set)
    keeps_short_addresses: bool = False

    def search_address(self) -> int:
        return (self.search[0] << 16) | (self.search[1] << 8) | self.search[2]

    def selected(self) -> list[int]:
        return [short for short, random in self.randoms.items() if random == self.search_address()]

    def compare(self) -> bool:
        return any(
            random <= self.search_address()
            for short, random in self.randoms.items()
            if short not in self.withdrawn
        )

    def clear_selected(self) -> None:
        if self.keeps_short_addresses:
            return
        for short in self.selected():
            self.unaddressed.add(self.randoms.pop(short))

    def move_selected(self, short: int) -> None:
        for old_short in self.selected():
            self.randoms[short] = self.randoms.pop(old_short)

    def apply(self, cmd: Command, general) -> None:
        """INITIALISE, TERMINATE or WITHDRAW."""
        if isinstance(cmd, general.Initialise):
            self.initialised = True
        elif isinstance(cmd, general.Terminate):
            self.initialised = False
            self.withdrawn.clear()
        elif self.initialised:
            self.withdrawn.update(self.selected())


class _HelvarBusDriver(FakeWBDALIDriver):  # pylint: disable=too-many-instance-attributes
    """A light at LIGHT with a GTIN, a Digidim sensor at SENSOR that answers of FF16 only QUERY
    CONTROL GEAR, and optionally a control device at SENSOR with `dali2_gtin` in bank 0. Both spaces
    take part in the device search; `ff17_fails` makes the gateway fail every FF17."""

    def __init__(
        self,
        sensor: Optional[_Sensor] = None,
        light: bool = False,
        dali2_gtin: Optional[int] = None,
    ) -> None:
        # Every memory byte of the light reads 0x12, a programmed GTIN among them.
        super().__init__(answers={"ReadMemoryLocation": 0x12})
        self.sensor = sensor
        self.ff17_fails = False
        self.gear = _SearchSpace()
        if light:
            self.gear.randoms[LIGHT] = LIGHT_RANDOM
        if sensor is not None:
            self.gear.randoms[SENSOR] = SENSOR_RANDOM
        self.devices = _SearchSpace()
        self.bank_0: Optional[bytes] = None
        if dali2_gtin is not None:
            self.devices.randoms[SENSOR] = SENSOR_RANDOM
            self.bank_0 = bytes(3) + dali2_gtin.to_bytes(6, "big")
        self.device_dtr0 = 0

    async def send(self, cmd, source=None, priority=None, logger=None):
        response = self._special_response(cmd)
        if response is None:
            return await super().send(cmd, source, priority, logger)
        self.sent.append(cmd)
        self.report(cmd, response, source)
        return response

    # --- Private ---

    def _special_response(self, cmd: Command) -> Optional[Response]:
        if len(cmd.frame) == FF17_BITS:
            return self._ff17_response(cmd)
        if isinstance(cmd, gear_general.DTR0) and self.sensor is not None:
            self.sensor.dtr0 = cmd.param
            return None
        response = self._search_response(cmd, self.gear, gear_general, GearShort)
        if response is None:
            response = self._search_response(cmd, self.devices, device_general, DeviceShort)
        if response is not None:
            return response
        if getattr(cmd, "destination", None) == GearShort(SENSOR) and self.sensor is not None:
            return cmd.response(None) if cmd.response is not None else Response(None)
        return self._device_memory_response(cmd)

    def _search_response(  # pylint: disable=too-many-return-statements
        self, cmd: Command, space: _SearchSpace, general, short_cls
    ) -> Optional[Response]:
        destination = getattr(cmd, "destination", None)
        present_query = (
            general.QueryControlGearPresent if general is gear_general else general.QueryDeviceStatus
        )
        if isinstance(cmd, present_query):
            return cmd.response(BackwardFrame(0xFF) if destination.address in space.randoms else None)
        for shift, query in (
            (16, general.QueryRandomAddressH),
            (8, general.QueryRandomAddressM),
            (0, general.QueryRandomAddressL),
        ):
            if isinstance(cmd, query) and isinstance(destination, short_cls):
                random = space.randoms.get(destination.address)
                return cmd.response(None if random is None else BackwardFrame((random >> shift) & 0xFF))
        search_classes = (
            (gear_general.SetSearchAddrH, gear_general.SetSearchAddrM, gear_general.SetSearchAddrL)
            if general is gear_general
            else (device_general.SearchAddrH, device_general.SearchAddrM, device_general.SearchAddrL)
        )
        for index, search_cls in enumerate(search_classes):
            if isinstance(cmd, search_cls):
                space.search[index] = cmd.param
                return Response(None)
        return self._search_state_response(cmd, space, general)

    def _search_state_response(self, cmd: Command, space: _SearchSpace, general) -> Optional[Response]:
        if isinstance(cmd, (general.Initialise, general.Terminate, general.Withdraw)):
            space.apply(cmd, general)
            return Response(None)
        if isinstance(cmd, general.ProgramShortAddress):
            if space.initialised:
                data = cmd.frame.as_integer & 0xFF
                if data == MASK:
                    space.clear_selected()
                else:
                    space.move_selected(data >> 1)
            return Response(None)
        if not space.initialised or not isinstance(cmd, (general.Compare, general.QueryShortAddress)):
            return None
        if isinstance(cmd, general.Compare):
            return cmd.response(BackwardFrame(0xFF) if space.compare() else None)
        selected = space.selected()
        if not selected:
            return cmd.response(BackwardFrame(MASK) if space.search_address() in space.unaddressed else None)
        return cmd.response(BackwardFrame((selected[0] << 1) | 1 if general is gear_general else selected[0]))

    def _device_memory_response(self, cmd: Command) -> Optional[Response]:
        if isinstance(cmd, device_general.DTR0):
            self.device_dtr0 = cmd.param
            return None
        if not isinstance(cmd, device_general.ReadMemoryLocation) or self.bank_0 is None:
            return None
        value = self.bank_0[self.device_dtr0] if self.device_dtr0 < len(self.bank_0) else None
        self.device_dtr0 += 1
        return cmd.response(None if value is None else BackwardFrame(value))

    def _ff17_response(self, cmd: Command) -> Response:
        if self.ff17_fails:
            return TransmissionFailed()
        frame = cmd.frame.as_integer
        # The 17th bit goes last and is always 0.
        assert frame & 1 == 0
        answer = None if self.sensor is None else self.sensor.answer(frame >> 1)
        backward = None if answer is None else BackwardFrame(answer)
        return cmd.response(backward) if cmd.response is not None else Response(None)


def _sensor_entry(**extra) -> dict:
    return {"short": SENSOR, "random": SENSOR_RANDOM, "helvar_digidim": True, **extra}


def _bus_data(*entries: dict, bus_monitor: bool = False) -> dict:
    return {"devices": list(entries), "bus_monitor_enabled": bus_monitor}


def _ff17_frames(driver: FakeWBDALIDriver) -> list[tuple[int, int]]:
    return [(FF17_BITS, c.frame.as_integer) for c in driver.sent if len(c.frame) == FF17_BITS]


def _sensor_frames(driver: FakeWBDALIDriver) -> list[tuple[int, int]]:
    """Frames to the sensor but its status polls, FF17 specials and DTR0, as (bits, frame) pairs."""
    return [
        (len(c.frame), c.frame.as_integer)
        for c in driver.sent
        if (
            getattr(c, "destination", None) in (GearShort(SENSOR), DeviceShort(SENSOR))
            and not isinstance(c, gear_general.QueryControlGearPresent)
        )
        or len(c.frame) == FF17_BITS
        or isinstance(c, gear_general.DTR0)
    ]


def _make_gateway(bus: ApplicationController, config_path: str) -> Gateway:
    gateway = Gateway({}, MagicMock(), config_path, MagicMock(), load_common_schema(), command_registry={})
    gateway.wb_dali_gateways = [WbDaliGateway(uid="gw1", buses=[bus])]
    return gateway


def _saved_devices(config_path: str) -> list[dict]:
    with open(config_path, encoding="utf-8") as config_file:
        return json.load(config_file)["gateways"][0]["buses"][0]["devices"]


async def _commission(bus: ApplicationController) -> None:
    await bus.start_commissioning()
    await asyncio.sleep(COMMISSIONING_S)


class HelvarFrameTest(VirtualTimeTestCase):
    def test_ff17_goes_to_the_gateway_with_size_code_3(self):
        """Read E2 to short address 6: the 16-bit word 0D 9A, a 0 after it and size code 3."""
        frame = ReadE2(GearShort(SENSOR)).frame

        encoded = encode_frame_for_modbus(frame, sendtwice=False, priority=FramePriority.AUTOMATIC)

        self.assertEqual(encoded, (4 << 29) | (3 << 25) | (0x0D9A << 1))


class HelvarConfigTest(VirtualTimeTestCase):
    def test_flag_selects_the_helvar_classes_and_is_saved_back(self):
        """An entry with `helvar_digidim` loads as the Digidim sensor and is written back with the
        flag; entries without it stay ordinary."""
        entries = [
            {"short": LIGHT, "random": LIGHT_RANDOM},
            _sensor_entry(name="Sensor"),
            {"short": LIGHT, "random": LIGHT_RANDOM, "dali2": True},
        ]
        bus = make_bus(_bus_data(*entries), FakeWBDALIDriver())

        self.assertEqual(
            [type(d) for d in bus.dali_devices + bus.dali2_devices],
            [DaliDevice, HelvarDigidimSensor, Dali2Device],
        )
        self.assertEqual(
            [get_dict_for_device_config(d) for d in bus.dali_devices + bus.dali2_devices], entries
        )


class HelvarCommissioningTest(VirtualTimeTestCase):
    async def test_new_311_is_saved_with_the_flag_and_published(self):
        """A new light with a GTIN and a new 311 without one: the 311 gets the Helvar check, each
        frame once, and is saved flagged with its model as the name; both are published."""
        driver = _HelvarBusDriver(_Sensor(), light=True)
        async with running_bus(_bus_data(), driver) as (bus, client):
            await _commission(bus)

            self.assertEqual(
                [get_dict_for_device_config(d) for d in bus.dali_devices if d.address.short == SENSOR],
                [_sensor_entry(name=f"Ceiling PIR Detector 311 {SENSOR}")],
            )
            self.assertEqual(_ff17_frames(driver), [f for f in IDENTIFICATION_FRAMES if f[0] == FF17_BITS])
            self.assertTrue(is_published(client, SENSOR_META))
            self.assertTrue(is_published(client, LIGHT_META))

    async def test_lost_e2_answer_is_retried_from_dtr(self):
        """A new 311 whose first Read E2 of byte 4 goes unanswered: the E2 read is repeated from DTR
        at byte 4 and the 311 is saved flagged."""
        sensor = _Sensor(lost_e2=4)
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(), driver) as (bus, _client):
            await _commission(bus)

            self.assertEqual(
                [get_dict_for_device_config(d) for d in bus.dali_devices],
                [_sensor_entry(name=f"Ceiling PIR Detector 311 {SENSOR}")],
            )
            frames = _sensor_frames(driver)
            self.assertEqual(
                frames[frames.index(MAJOR_VERSION_FRAME) :],
                IDENTIFICATION_FRAMES + [_dtr_frame(4), *IDENTIFICATION_FRAMES[3:10]],
            )
            self.assertEqual(sensor.read_addresses[-6:], IDENTITY_E2[4:])

    async def test_gear_failing_the_check_stays_ordinary(self):
        """No answer to the major version, a gateway error on FF17, Read E2 ending inside the GTIN
        or a GTIN that is not Helvar: ordinary gear without the flag; E5 is repeated only when it fails."""
        cases = {
            "no major version": (_Sensor(silent_opcodes={0xE5}), False, MAX_COMMAND_RETRIES),
            "gateway error on FF17": (_Sensor(), True, MAX_COMMAND_RETRIES),
            "E2 read ends inside the GTIN": (_Sensor(readable_e2=4), False, 1),
            "GTIN is not Helvar": (_Sensor(e2=bytearray(_e2(GTIN_PHILIPS_302))), False, 1),
        }
        for case, (sensor, ff17_fails, major_version_count) in cases.items():
            with self.subTest(case):
                driver = _HelvarBusDriver(sensor)
                driver.ff17_fails = ff17_fails
                async with running_bus(_bus_data(), driver) as (bus, client):
                    await _commission(bus)

                    self.assertEqual([type(d) for d in bus.dali_devices], [DaliDevice])
                    self.assertNotIn("helvar_digidim", get_dict_for_device_config(bus.dali_devices[0]))
                    self.assertEqual(_ff17_frames(driver).count(MAJOR_VERSION_FRAME), major_version_count)
                    self.assertTrue(is_published(client, SENSOR_META))

    async def test_new_control_device_with_321_gtin_loses_its_address(self):
        """A new control device whose bank 0 GTIN is the 321's: its DALI-2 short address is cleared,
        it is not saved or published, and an INFO line says so."""
        driver = _HelvarBusDriver(dali2_gtin=GTIN_321)
        with self.assertLogs(BUS_UID, logging.INFO) as logs:
            async with running_bus(_bus_data(), driver) as (bus, client):
                await _commission(bus)

                self.assertEqual(bus.dali2_devices, [])
                self.assertEqual(driver.devices.unaddressed, {SENSOR_RANDOM})
                self.assertFalse(is_published(client, DALI2_SENSOR_META))

        self.assertIn(
            f"Helvar 321 at DALI-2 address {SENSOR}: address cleared",
            [r.getMessage() for r in logs.records if r.levelno == logging.INFO],
        )

    async def test_321_keeping_its_dali2_address_is_skipped_with_a_warning(self):
        """A new 321 among control devices that ignores the clearing: it keeps its address, is not
        saved or published, and a WARNING says so."""
        driver = _HelvarBusDriver(dali2_gtin=GTIN_321)
        driver.devices.keeps_short_addresses = True
        with self.assertLogs(BUS_UID, logging.WARNING) as logs:
            async with running_bus(_bus_data(), driver) as (bus, client):
                await _commission(bus)

                self.assertEqual(bus.dali2_devices, [])
                self.assertEqual(driver.devices.randoms, {SENSOR: SENSOR_RANDOM})
                self.assertFalse(is_published(client, DALI2_SENSOR_META))

        self.assertTrue(any("Helvar 321 at DALI-2 address" in r.getMessage() for r in logs.records))


class HelvarStartTest(VirtualTimeTestCase):
    async def test_flagged_sensor_is_published_with_its_status_only(self):
        """A flagged sensor: published with `error_status` alone, polled only by QUERY CONTROL GEAR
        PRESENT: 0 «Норма» while it answers, 1 «Не отвечает» while silent."""
        for case, sensor, value, title in (
            ("answers", _Sensor(), "0", "Норма"),
            ("silent", None, "1", "Не отвечает"),
        ):
            with self.subTest(case):
                driver = _HelvarBusDriver(sensor)
                async with running_bus(_bus_data(_sensor_entry()), driver) as (_bus, client):
                    await asyncio.sleep(BACKGROUND_S)

                    self.assertTrue(is_published(client, SENSOR_META))
                    self.assertEqual(
                        {
                            p.topic.split("/")[4]
                            for p in client.publishes
                            if p.topic.startswith(SENSOR_CONTROLS)
                        },
                        {"error_status"},
                    )
                    self.assertEqual(control_values(client, SENSOR_ID, "error_status")[-1], value)
                    self.assertEqual(
                        json.loads(payloads(client, f"{SENSOR_CONTROLS}error_status/meta")[-1])["title"][
                            "ru"
                        ],
                        title,
                    )
                    self.assertIsNone(control_error(client, SENSOR_ID, "error_status"))
                    sensor_frames = [
                        c for c in driver.sent if getattr(c, "destination", None) == GearShort(SENSOR)
                    ]
                    self.assertTrue(sensor_frames)
                    self.assertTrue(
                        all(isinstance(c, gear_general.QueryControlGearPresent) for c in sensor_frames)
                    )


class _EditorTestCase(VirtualTimeTestCase):
    def setUp(self) -> None:
        config_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, config_dir)
        self.config_path = os.path.join(config_dir, "wb-mqtt-dali.conf")


class HelvarSensorEditorTest(_EditorTestCase):
    async def test_get_device_of_311_reads_its_identity(self):
        """GetDevice of a flagged 311: version, GTIN and serial number read from the sensor, brand and
        model with them; the note says the sensor controls other devices itself."""
        driver = _HelvarBusDriver(_Sensor(e2=_image()))
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)

            reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            identity_keys = (
                "short_address",
                "random_address",
                "name",
                "brand_name",
                "product_name",
                "gtin",
                "identification_number",
            )
            self.assertEqual(
                {key: reply["config"][key] for key in (*identity_keys, "firmware_version")},
                {
                    "short_address": SENSOR,
                    "random_address": hex(SENSOR_RANDOM),
                    "name": "DALI 6",
                    "brand_name": "Helvar",
                    "product_name": "Ceiling PIR Detector 311",
                    "gtin": 5030797003118,
                    "identification_number": "001432500",
                    "firmware_version": "6.08",
                },
            )
            self.assertEqual(
                reply["schema"]["description"],
                "The sensor sends no events and does not obey DALI commands: it controls other devices "
                "itself by the state of its sensors, as set below",
            )
            self.assertEqual(_sensor_frames(driver)[: len(IDENTIFICATION_FRAMES)], IDENTIFICATION_FRAMES)

    async def test_get_device_of_other_helvar_model_says_it_is_not_supported(self):
        """GetDevice of a 312 and of another Helvar GTIN: the brand without a model, the note that
        the model is not supported and no settings; only the identification is sent."""
        cases = {
            "312": _e2(GTIN_312),
            "unlisted Helvar GTIN": _e2(GTIN_UNLISTED_HELVAR),
        }
        for case, e2 in cases.items():
            with self.subTest(case):
                driver = _HelvarBusDriver(_Sensor(e2=bytearray(e2)))
                async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
                    gateway = _make_gateway(bus, self.config_path)

                    reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

                    self.assertEqual(reply["config"]["brand_name"], "Helvar")
                    self.assertNotIn("product_name", reply["config"])
                    self.assertEqual(reply["config"]["firmware_version"], "6.08")
                    self.assertNotIn("on_time_s", reply["config"])
                    self.assertIn("description", reply["schema"])
                    self.assertEqual(_sensor_frames(driver), IDENTIFICATION_FRAMES)

    async def test_repeated_get_device_reads_the_sensor_only_with_force_reload(self):
        """A second GetDevice of a 311 answers from memory without frames to the sensor; with
        forceReload it reads the identity and settings again."""
        driver = _HelvarBusDriver(_Sensor(e2=_image()))
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            first = await gateway.get_device_rpc_handler({"deviceId": device_id})
            driver.sent.clear()

            second = await gateway.get_device_rpc_handler({"deviceId": device_id})
            self.assertEqual(_sensor_frames(driver), [])

            reloaded = await gateway.get_device_rpc_handler({"deviceId": device_id, "forceReload": True})
            self.assertEqual(_sensor_frames(driver)[: len(IDENTIFICATION_FRAMES)], IDENTIFICATION_FRAMES)
            self.assertEqual(second, first)
            self.assertEqual(reloaded, first)

    async def test_get_device_of_silent_sensor_fails(self):
        """The sensor does not answer the major version: GetDevice raises."""
        driver = _HelvarBusDriver(_Sensor(silent_opcodes={0xE5}))
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)

            with self.assertRaises(RuntimeError):
                await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

    async def test_set_device_renames_sensor(self):
        """SetDevice of a 312 with the form GetDevice gave and a new name: the name and the flag are
        saved, the MQTT title follows, nothing but status polls goes to the sensor."""
        driver = _HelvarBusDriver(_Sensor(e2=bytearray(_e2(GTIN_312))))
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]
            driver.sent.clear()

            reply = await gateway.set_device_rpc_handler(
                {"deviceId": device_id, "config": {**form, "name": "Sensor"}}
            )
            await asyncio.sleep(SETTLE_S)

            self.assertEqual(_saved_devices(self.config_path), [_sensor_entry(name="Sensor")])
            self.assertEqual(reply, {**form, "name": "Sensor"})
            self.assertEqual(_sensor_frames(driver), [])
            self.assertEqual(json.loads(payloads(client, SENSOR_META)[-1])["title"]["en"], "Sensor")

    async def test_set_device_moves_sensor_to_new_short_address(self):
        """SetDevice of a 312 with a new short address: the sensor is programmed through INITIALISE,
        the new address with the auto name and MQTT id is saved and published."""
        driver = _HelvarBusDriver(_Sensor(e2=bytearray(_e2(GTIN_312))))
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]

            reply = await gateway.set_device_rpc_handler(
                {"deviceId": device_id, "config": {**form, "short_address": NEW_SENSOR}}
            )
            await asyncio.sleep(SETTLE_S)

            self.assertEqual(driver.gear.randoms, {NEW_SENSOR: SENSOR_RANDOM})
            self.assertEqual(_saved_devices(self.config_path), [_sensor_entry(short=NEW_SENSOR)])
            self.assertEqual(
                reply,
                {
                    **form,
                    "short_address": NEW_SENSOR,
                    "name": f"DALI {NEW_SENSOR}",
                    "mqtt_id": f"{BUS_UID}_{NEW_SENSOR}",
                },
            )
            self.assertFalse(is_published(client, SENSOR_META))
            self.assertTrue(is_published(client, f"/devices/{BUS_UID}_{NEW_SENSOR}/meta"))


class HelvarSettingsTest(_EditorTestCase):
    async def test_get_device_of_311_reads_and_decodes_its_settings(self):
        """GetDevice of a 311 6.08 with the dumped E2: past the identity only the form bytes are read,
        a DTR per run; 20-s steps, inverted DIP bits, IR bytes, presence, touch and IR remote events."""
        sensor = _Sensor(e2=_image())
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)

            reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            self.assertEqual(sensor.read_addresses, IDENTITY_E2 + E2_SETTINGS)
            self.assertEqual(
                [frame for frame in _sensor_frames(driver) if frame[0] == 16],
                [_dtr_frame(a) for a in (0x00, 0x0B, 0x20, 0x30, 0x54)],
            )
            config = reply["config"]
            self.assertEqual(
                {
                    key: config[key]
                    for key in config
                    if key
                    not in (
                        "name",
                        "mqtt_id",
                        "short_address",
                        "random_address",
                        "brand_name",
                        "product_name",
                    )
                    and key not in ("gtin", "identification_number", "firmware_version")
                },
                {
                    "sensitivity": 7,
                    "on_time_s": 60,
                    "transition_s": 20,
                    "exit_delay_s": 120,
                    "scene_store_delay_s": 10,
                    "lock": False,
                    "presence_sensor": True,
                    "ir_receiver": True,
                    "ir_disabled": False,
                    "ir_address_group": 0xFF,
                    "presence_test_mode": False,
                    "presence_on_event": _event("broadcast", "scene", scene=15, fade_time=4),
                    "presence_min_event": _event("broadcast", "scene", scene=13, fade_time=4),
                    "presence_off_event": _event("broadcast", "scene", scene=14, fade_time=4),
                    "touch_switch_event": _event("broadcast", "touch_last_level"),
                    **{
                        f"ir_selection_{button}_event": _event(
                            "broadcast", "scene_recall_store", scene=button - 1, fade_time=4
                        )
                        for button in range(1, 9)
                    },
                    "ir_wall_panel_right_event": _event("broadcast", "off"),
                    "ir_wall_panel_left_event": _event("broadcast", "scene"),
                    "ir_on_off_event": _event("broadcast", "last_level_off"),
                    "ir_up_event": _event("broadcast", "up_step_up", fade_time=0),
                    "ir_down_event": _event("broadcast", "down_step_down", fade_time=0),
                },
            )
            self.assertNotIn("constant_light", reply["schema"]["properties"])

    async def test_get_device_of_321_decodes_targets_commands_and_fades(self):
        """GetDevice of a 321: no sensitivity or IR receiver, Constant light shown; a group target, an
        address target with a fade index and «do not change», a DALI query shown as an unrecognised command.
        """
        sensor = _Sensor(
            e2=_image(
                GTIN_321,
                e2_0b=b"\x98",
                e2_26=b"\xda",
                e2_36=bytes.fromhex("00 10 93 13 02 00"),
                e2_3c=bytes.fromhex("80 07 0d 06 00 00"),
                e2_42=UNRECOGNISED_SLOT,
            )
        )
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)

            reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            config = reply["config"]
            self.assertNotIn(0x20, sensor.read_addresses)
            self.assertNotIn("sensitivity", reply["schema"]["properties"])
            self.assertNotIn("ir_receiver", reply["schema"]["properties"])
            self.assertEqual(config["product_name"], "MultiSensor 321")
            self.assertEqual((config["lock"], config["scene_store_delay_s"]), (True, 24))
            self.assertEqual(
                (config["presence_sensor"], config["constant_light"], config["presence_test_mode"]),
                (True, True, True),
            )
            self.assertEqual(config["presence_on_event"], _event("group", "scene", group=9, scene=3))
            self.assertEqual(
                config["presence_min_event"], _event("address", "recall_min", address=6, fade_time=7)
            )
            self.assertEqual(config["presence_off_event"]["command"], "unrecognised")
            self.assertIn(
                "unrecognised",
                reply["schema"]["properties"]["presence_off_event"]["properties"]["command"]["enum"],
            )

    async def test_get_device_decodes_direct_level_ff17_and_step_commands(self):
        """Events holding Direct Level 128 to address 6, FF17 Enable PIR Off and Step Up: decoded, and
        «unrecognised» is offered only for the event that holds one."""
        sensor = _Sensor(
            e2=_image(
                e2_36=bytes.fromhex("80 04 0c 80 00 00"),
                e2_3c=bytes.fromhex("08 00 ff 31 00 00"),
                e2_42=bytes.fromhex("00 10 ff 03 00 00"),
            )
        )
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)

            reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            config = reply["config"]
            self.assertEqual(
                config["presence_on_event"],
                _event("address", "direct_level", address=6, level=128, fade_time=4),
            )
            self.assertEqual(config["presence_min_event"], _event("broadcast", "enable_pir_off"))
            self.assertEqual(config["presence_off_event"], _event("broadcast", "step_up"))
            for key in ("presence_on_event", "presence_min_event", "presence_off_event"):
                commands = reply["schema"]["properties"][key]["properties"]["command"]["enum"]
                self.assertNotIn("unrecognised", commands)

    async def test_old_311_has_no_sensitivity_and_no_ir_receiver(self):
        """A 311 with firmware 5.10: no Sensitivity or IR receiver in the form; E2 0x20 is neither read
        nor written, also when SetDevice changes On time."""
        sensor = _Sensor(e2=_image(), firmware=(5, 10))
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid

            reply = await gateway.get_device_rpc_handler({"deviceId": device_id})
            await gateway.set_device_rpc_handler(
                {"deviceId": device_id, "config": {**reply["config"], "on_time_s": 120}}
            )

            self.assertEqual(reply["config"]["firmware_version"], "5.10")
            self.assertEqual(sensor.read_addresses, IDENTITY_E2 + E2_SETTINGS_OLD_311)
            for key in ("sensitivity", "ir_receiver", "constant_light"):
                self.assertNotIn(key, reply["config"])
                self.assertNotIn(key, reply["schema"]["properties"])
            self.assertEqual(
                [i for i, (old, new) in enumerate(zip(_image(), sensor.e2)) if old != new], [0x21]
            )

    async def test_set_device_writes_only_changed_bytes(self):
        """SetDevice changing On time, Exit delay (0x21, 0x22) and Lock (0x0B): DTR and Extended DTR MSB
        per run, Enable Write E2 + Write E2 per byte; nothing else of E2 changes, nothing is read."""
        sensor = _Sensor(e2=_image())
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]
            driver.sent.clear()

            reply = await gateway.set_device_rpc_handler(
                {
                    "deviceId": device_id,
                    "config": {**form, "on_time_s": 300, "exit_delay_s": 5100, "lock": True},
                }
            )

            self.assertEqual(
                _sensor_frames(driver),
                [
                    _dtr_frame(0x0B),
                    IDENTIFICATION_FRAMES[3],
                    ENABLE_WRITE_E2_FRAME,
                    _write_frame(0x8A),
                    _dtr_frame(0x21),
                    IDENTIFICATION_FRAMES[3],
                    ENABLE_WRITE_E2_FRAME,
                    _write_frame(15),
                    ENABLE_WRITE_E2_FRAME,
                    _write_frame(255),
                ],
            )
            self.assertEqual(sensor.e2, _image(e2_0b=b"\x8a", e2_21=bytes((15, 255))))
            self.assertEqual((reply["on_time_s"], reply["exit_delay_s"], reply["lock"]), (300, 5100, True))

    async def test_set_device_rounds_duration_to_nearest_step(self):
        """SetDevice with On time 129 s and Exit delay 130 s: 120 and 140 s are written, the reply
        shows them."""
        sensor = _Sensor(e2=_image())
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]

            reply = await gateway.set_device_rpc_handler(
                {"deviceId": device_id, "config": {**form, "on_time_s": 129, "exit_delay_s": 130}}
            )

            self.assertEqual(sensor.e2, _image(e2_21=bytes((6, 7))))
            self.assertEqual((reply["on_time_s"], reply["exit_delay_s"]), (120, 140))

    async def test_set_device_keeps_dip_bits_outside_the_form(self):
        """Switching the Presence sensor off sets only bit 2 of E2 0x26: the bits hidden for the model
        (Constant light on a 311, IR receiver on a 321) and unknown bits 3, 4, 6, 7 stay as read."""
        cases = {"311": (5030797003118, 0x58), "321": (GTIN_321, 0xD9)}
        for model, (gtin, dip) in cases.items():
            with self.subTest(model):
                sensor = _Sensor(e2=_image(gtin, e2_26=bytes((dip,))))
                driver = _HelvarBusDriver(sensor)
                async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
                    gateway = _make_gateway(bus, self.config_path)
                    device_id = bus.dali_devices[0].uid
                    form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]

                    await gateway.set_device_rpc_handler(
                        {"deviceId": device_id, "config": {**form, "presence_sensor": False}}
                    )

                    self.assertEqual(sensor.e2[0x26], dip | 0x04)

    async def test_set_device_writes_direct_level_and_ff17_command(self):
        """SetDevice with Direct Level 100 to group 2 and FF17 Disable Panel to all: the Direct Level slot
        gets the group byte with bit 0 cleared, the FF17 one mode 0x08 and no fade byte."""
        sensor = _Sensor(e2=_image())
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]

            reply = await gateway.set_device_rpc_handler(
                {
                    "deviceId": device_id,
                    "config": {
                        **form,
                        "presence_on_event": _event("group", "direct_level", group=2, level=100),
                        "presence_off_event": _event("broadcast", "disable_panel"),
                    },
                }
            )

            self.assertEqual(sensor.e2[PRESENCE_ON : PRESENCE_ON + 6], bytes.fromhex("00 10 84 64 00 04"))
            self.assertEqual(sensor.e2[PRESENCE_OFF : PRESENCE_OFF + 6], bytes.fromhex("08 00 ff 8e 00 00"))
            self.assertEqual(reply["presence_on_event"], _event("group", "direct_level", group=2, level=100))
            self.assertEqual(reply["presence_off_event"], _event("broadcast", "disable_panel"))

    async def test_get_device_of_321_offers_ramp_only_for_constant_light(self):
        """A 321 with the dumped slots: Constant light output shows the regulation command, offered only
        there; the touch switch is in the form, the IR settings and buttons are not."""
        driver = _HelvarBusDriver(_Sensor(e2=_image(GTIN_321)))
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)

            reply = await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            properties = reply["schema"]["properties"]
            self.assertEqual(reply["config"]["constant_light_event"], _event("broadcast", "ramp"))
            self.assertIn("ramp", properties["constant_light_event"]["properties"]["command"]["enum"])
            self.assertNotIn("ramp", properties["presence_on_event"]["properties"]["command"]["enum"])
            self.assertEqual(reply["config"]["touch_switch_event"], _event("broadcast", "touch_last_level"))
            for key in ("ir_disabled", "ir_address_group", "ir_up_event"):
                self.assertNotIn(key, properties)

    async def test_get_device_shows_unknown_ir_address_group_as_it_is_and_keeps_it(self):
        """IR address group 0x20, outside the offered codes: shown as read with its hex as the title;
        SetDevice changing Lock leaves it as it was."""
        sensor = _Sensor(e2=_image(e2_0c=b"\x20"))
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            reply = await gateway.get_device_rpc_handler({"deviceId": device_id})

            await gateway.set_device_rpc_handler(
                {"deviceId": device_id, "config": {**reply["config"], "lock": True}}
            )

            properties = reply["schema"]["properties"]
            self.assertEqual(reply["config"]["ir_address_group"], 0x20)
            self.assertEqual(properties["ir_address_group"]["options"]["enum_titles"][-1], "0x20")
            self.assertEqual(sensor.e2, _image(e2_0b=b"\x8a\x20"))

    async def test_set_device_writes_ir_settings_and_button_commands(self):
        """SetDevice disabling IR, with IR address group A and button
        commands: 0x0E gets 0xF2, the slots get the bytes of the commands."""
        sensor = _Sensor(e2=_image())
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]

            await gateway.set_device_rpc_handler(
                {
                    "deviceId": device_id,
                    "config": {
                        **form,
                        "ir_disabled": True,
                        "ir_address_group": 0x0A,
                        "ir_selection_1_event": _event("broadcast", "scene_recall_store", scene=5),
                        "ir_on_off_event": _event("broadcast", "scene_toggle", scene=11, fade_time=2),
                        "ir_up_event": _event("address", "touch_scene_0", address=3),
                        "ir_down_event": _event("broadcast", "up_step_up_and_on"),
                    },
                }
            )

            self.assertEqual(
                sensor.e2,
                _image(
                    e2_0c=b"\x0a",
                    e2_0e=b"\xf2",
                    e2_54=bytes.fromhex("01 10 ff 15 00 00"),
                    e2_90=bytes.fromhex("83 02 ff 1b 00 00"),
                    e2_96=bytes.fromhex("02 00 07 10 00 00"),
                    e2_9c=bytes.fromhex("05 08 ff 01 00 00"),
                ),
            )

    async def test_set_device_rejects_toggle_of_scene_12(self):
        """SetDevice with «scene or off in turn» on scene 12, which the command cannot hold: the RPC fails
        and E2 is not written."""
        sensor = _Sensor(e2=_image())
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]

            with self.assertRaises(ValueError):
                await gateway.set_device_rpc_handler(
                    {
                        "deviceId": device_id,
                        "config": {
                            **form,
                            "lock": True,
                            "ir_on_off_event": _event("broadcast", "scene_toggle", scene=12),
                        },
                    }
                )

            self.assertEqual(sensor.e2, _image())

    async def test_set_device_keeps_unchanged_other_command_slot(self):
        """Presence Off holds a DALI query shown as an unrecognised command; SetDevice moving Presence On to
        group 2 rewrites its target and Responds to Groups only, the query slot stays as it was."""
        sensor = _Sensor(e2=_image(e2_42=UNRECOGNISED_SLOT))
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]
            driver.sent.clear()

            await gateway.set_device_rpc_handler(
                {
                    "deviceId": device_id,
                    "config": {
                        **form,
                        "presence_on_event": {**form["presence_on_event"], "target": "group", "group": 2},
                    },
                }
            )

            self.assertEqual(form["presence_off_event"]["command"], "unrecognised")
            self.assertEqual(sensor.e2[PRESENCE_OFF : PRESENCE_OFF + 6], UNRECOGNISED_SLOT)
            self.assertEqual(sensor.e2[PRESENCE_ON : PRESENCE_ON + 6], bytes.fromhex("8004851f0004"))
            self.assertEqual(
                [frame for frame in _sensor_frames(driver) if frame[0] == 16],
                [_dtr_frame(PRESENCE_ON + 2), _dtr_frame(PRESENCE_ON + 5)],
            )

    async def test_set_device_keeps_out_of_range_bytes_of_unchanged_fields(self):
        """On time 0 (shown 60 s) and scene store delay 64 (shown 25 s): SetDevice changing Exit delay
        and Lock writes those, 0x21 stays 0 and the delay bits of 0x0B stay 64."""
        sensor = _Sensor(e2=_image(e2_0b=b"\x40", e2_21=b"\x00"))
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]

            await gateway.set_device_rpc_handler(
                {"deviceId": device_id, "config": {**form, "exit_delay_s": 300, "lock": True}}
            )

            self.assertEqual((form["on_time_s"], form["scene_store_delay_s"]), (60, 25))
            self.assertEqual(sensor.e2, _image(e2_0b=b"\xc0", e2_21=bytes((0, 15))))

    async def test_set_device_fails_on_gateway_error(self):
        """The gateway fails the FF17 frames of the write: SetDevice raises and the name is not saved."""
        driver = _HelvarBusDriver(_Sensor(e2=_image()))
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]
            driver.ff17_fails = True

            with self.assertRaises(RuntimeError):
                await gateway.set_device_rpc_handler(
                    {"deviceId": device_id, "config": {**form, "name": "Sensor", "on_time_s": 120}}
                )

            self.assertFalse(os.path.exists(self.config_path))

    async def test_set_device_after_failed_write_reads_the_sensor_again(self):
        """A write fails after On time reached the sensor; SetDevice of the old form then reads E2 again,
        writes the old On time back and replies with it."""
        sensor = _Sensor(e2=_image())
        driver = _HelvarBusDriver(sensor)
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            device_id = bus.dali_devices[0].uid
            form = (await gateway.get_device_rpc_handler({"deviceId": device_id}))["config"]
            driver.ff17_fails = True
            with self.assertRaises(RuntimeError):
                await gateway.set_device_rpc_handler(
                    {"deviceId": device_id, "config": {**form, "on_time_s": 120}}
                )
            sensor.e2[0x21] = 6
            driver.ff17_fails = False
            sensor.read_addresses.clear()

            reply = await gateway.set_device_rpc_handler({"deviceId": device_id, "config": form})

            self.assertIn(0x21, sensor.read_addresses)
            self.assertEqual(sensor.e2, _image())
            self.assertEqual(reply["on_time_s"], form["on_time_s"])


class HelvarEditorActionsTest(_EditorTestCase):
    async def test_identify_of_sensor_initialises_it_for_a_while(self):
        """Identify of a flagged sensor: INITIALISE to its address, TERMINATE 5 s later, nothing else
        on the bus; the RPC returns after TERMINATE."""
        driver = _HelvarBusDriver(_Sensor())
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)
            await asyncio.sleep(SETTLE_S)
            sent_before = len(driver.sent)
            started_at = asyncio.get_running_loop().time()

            await gateway.identify_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            self.assertGreaterEqual(asyncio.get_running_loop().time() - started_at, IDENTIFY_S)
            self.assertEqual(
                [(len(c.frame), c.frame.as_integer) for c in driver.sent[sent_before:]],
                [INITIALISE_SENSOR_FRAME, TERMINATE_FRAME],
            )
            self.assertFalse(os.path.exists(self.config_path))

    async def test_reset_settings_keeps_the_helvar_class(self):
        """Reset settings of a flagged sensor: the RPC succeeds, the device stays of its class and
        published and gets no frame but its status polls."""
        driver = _HelvarBusDriver(_Sensor())
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, client):
            gateway = _make_gateway(bus, self.config_path)
            [device] = bus.dali_devices

            await gateway.reset_device_settings_rpc_handler({"deviceId": device.uid})
            await asyncio.sleep(BACKGROUND_S)

            self.assertEqual(_sensor_frames(driver), [])
            [new_device] = bus.dali_devices
            self.assertIs(type(new_device), HelvarDigidimSensor)
            self.assertEqual(new_device.uid, device.uid)
            self.assertTrue(is_published(client, SENSOR_META))

    async def test_remove_of_sensor_clears_its_address_without_reset(self):
        """ResetDevice of a flagged sensor: no RESET, its short address cleared, the entry dropped
        from the bus and the saved config."""
        driver = _HelvarBusDriver(_Sensor())
        async with running_bus(_bus_data(_sensor_entry()), driver) as (bus, _client):
            gateway = _make_gateway(bus, self.config_path)

            await gateway.reset_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})

            self.assertFalse(
                any(isinstance(c, (gear_general.Reset, device_general.Reset)) for c in driver.sent)
            )
            self.assertEqual(driver.gear.randoms, {})
            self.assertEqual(driver.gear.unaddressed, {SENSOR_RANDOM})
            self.assertEqual(bus.dali_devices, [])
            self.assertEqual(_saved_devices(self.config_path), [])


class HelvarBusMonitorTest(_EditorTestCase):
    async def test_own_ff17_is_shown_as_hex_with_its_answer(self):
        """Own FF17 frames of GetDevice go to the monitor as hex, `FF17` and the answer."""
        driver = _HelvarBusDriver(_Sensor())
        async with running_bus(_bus_data(_sensor_entry(), bus_monitor=True), driver) as (bus, client):
            gateway = _make_gateway(bus, self.config_path)
            await gateway.get_device_rpc_handler({"deviceId": bus.dali_devices[0].uid})
            await asyncio.sleep(SETTLE_S)

            bodies = [line.partition(" ")[2] for line in payloads(client, BUS_MONITOR_TOPIC) if line]

            self.assertIn(">> 001bca FF17 - 0006 6", bodies)
            self.assertIn(">> 001b34 FF17 - 0050 80", bodies)
