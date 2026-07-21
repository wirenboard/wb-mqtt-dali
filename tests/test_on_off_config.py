"""Config-side and Editor-RPC behaviour of the ``on_off`` feature.

Covers the validation split (ranges by JSON schema, requiredness by the parser),
round-trip through ``save_configuration``, the post-scan pruning of vanished groups'
settings, the silent skip on dali2 entries, duplicate group numbers, and the
SetDevice/SetGroup/GetGroup editor flows with the ``enabled`` RPC representation.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
from dali.address import GearShort

from wb.mqtt_dali.application_controller import ApplicationControllerState
from wb.mqtt_dali.commissioning import CommissioningResult
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.config_validator import validate_config
from wb.mqtt_dali.control_ids import ON_OFF
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveType
from wb.mqtt_dali.gateway import (
    Gateway,
    WbDaliGateway,
    bus_from_json,
    save_configuration,
)
from wb.mqtt_dali.on_off_control import (
    OnOffControl,
    OnOffSettingsParam,
    on_off_config_from_json,
    on_off_config_to_json,
    on_off_editor_schema,
)
from wb.mqtt_dali.utils import merge_json_schemas
from wb.mqtt_dali.virtual_devices import AggregatedCapabilities, BroadcastVirtualDevice

from ._app_controller_helpers import make_loop_controller, stop_loop
from ._on_off_helpers import ScriptedDriver

DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access

_SCHEMA = json.loads(
    (Path(__file__).resolve().parent.parent / "wb-mqtt-dali.schema.json").read_text(encoding="utf-8")
)

VALID_ON_OFF = {
    "on_action": {"mode": "level", "percent": 50, "fade_time": 3},
    "off_action": {"mode": "dapc", "fade_time": 1},
}
SCENE_ON_OFF = {
    "on_action": {"mode": "scene", "scene": 2},
    "off_action": {"mode": "off"},
}


def _config(device_on_off=None, groups=None, dali2=False) -> dict:
    device = {"short": 0, "random": 1}
    if dali2:
        device["dali2"] = True
    if device_on_off is not None:
        device["on_off"] = device_on_off
    bus = {"devices": [device]}
    if groups is not None:
        bus["groups"] = groups
    return {"gateways": [{"device_id": "gw", "buses": [bus]}]}


def _validate(config: dict) -> None:
    jsonschema.validate(instance=config, schema=_SCHEMA)


class _GearStub:  # pylint: disable=too-many-instance-attributes
    """Initialized-gear stand-in with the public surface commissioning/save touch."""

    def __init__(self, short: int, groups, is_initialized=True) -> None:
        self.address = DaliDeviceAddress(short, 0xABCDEF + short)
        self.uid = f"uid-{short}"
        self.mqtt_id = f"gw1_bus_1_{short}"
        self.name = f"DALI {short}"
        self.groups = set(groups)
        self.is_initialized = is_initialized
        self.dt8_colour_type = None
        self.dt8_tc_limits = None
        self.dimming_curve_type = DimmingCurveType.LOGARITHMIC
        self.has_custom_mqtt_id = False
        self.has_custom_name = False

    def get_group_state_controls(self):
        return []

    def set_logger(self, logger):
        pass


def _save_to_temp(gateways) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wb-mqtt-dali.conf")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        save_configuration(path, debug=False, gateways=gateways)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


class OnOffParserTest(unittest.TestCase):

    def test_missing_mode_fields_rejected(self):
        """A missing required field of the selected mode — and a missing mode or action
        object — is rejected by the parser (the schema has no ``required`` at all)."""
        for on_action in ({"mode": "scene"}, {"mode": "level"}, {"mode": "dapc"}, {}):
            with self.subTest(on_action=on_action):
                on_off = {"on_action": on_action, "off_action": {"mode": "off"}}
                _validate(_config(device_on_off=on_off))  # schema-valid on purpose
                with self.assertRaises(ValueError):
                    on_off_config_from_json(on_off)
        with self.subTest(off_action={}):
            on_off = {"on_action": {"mode": "scene", "scene": 1}, "off_action": {}}
            with self.assertRaises(ValueError):
                on_off_config_from_json(on_off)
        with self.subTest(missing="off_action"):
            with self.assertRaises(ValueError):
                on_off_config_from_json({"on_action": {"mode": "scene", "scene": 1}})

    def test_boolean_action_field_rejected(self):
        """A JSON ``true`` in an integer action field is rejected: bool is an int
        subclass, so without the guard ``{"scene": true}`` would parse to scene 1."""
        with self.assertRaises(ValueError):
            on_off_config_from_json(
                {"on_action": {"mode": "scene", "scene": True}, "off_action": {"mode": "off"}}
            )

    def test_parser_accepts_all_modes_and_ignores_foreign_fields(self):
        """Every on/off mode parses; fade_time is optional; fields of a foreign mode are
        ignored and dropped from the canonical serialization."""
        cases = [
            ({"mode": "scene", "scene": 3, "percent": 50, "value": 10}, {"mode": "scene", "scene": 3}),
            ({"mode": "last_active_level", "scene": 3}, {"mode": "last_active_level"}),
            ({"mode": "last_active_level", "fade_time": 2}, {"mode": "last_active_level", "fade_time": 2}),
            ({"mode": "level", "percent": 60, "value": 10, "scene": 1}, {"mode": "level", "percent": 60}),
            ({"mode": "dapc", "value": 200, "percent": 60}, {"mode": "dapc", "value": 200}),
        ]
        for on_action, canonical in cases:
            with self.subTest(on_action=on_action):
                config = on_off_config_from_json({"on_action": on_action, "off_action": {"mode": "off"}})
                self.assertEqual(on_off_config_to_json(config)["on_action"], canonical)
        off_cases = [
            ({"mode": "off", "fade_time": 3, "scene": 1}, {"mode": "off"}),
            ({"mode": "dapc"}, {"mode": "dapc"}),
            ({"mode": "dapc", "fade_time": 4, "percent": 10}, {"mode": "dapc", "fade_time": 4}),
        ]
        for off_action, canonical in off_cases:
            with self.subTest(off_action=off_action):
                config = on_off_config_from_json(
                    {"on_action": {"mode": "scene", "scene": 1}, "off_action": off_action}
                )
                self.assertEqual(on_off_config_to_json(config)["off_action"], canonical)


class OnOffConfigValidatorTest(unittest.TestCase):

    def test_duplicate_group_numbers_rejected(self):
        groups = [{"number": 3, "on_off": VALID_ON_OFF}, {"number": 3, "on_off": SCENE_ON_OFF}]
        with self.assertRaisesRegex(ValueError, "Duplicate group 3"):
            validate_config(_config(groups=groups))
        validate_config(_config(groups=[{"number": 3, "on_off": VALID_ON_OFF}]))

    def test_malformed_on_off_rejected_at_config_load(self):
        """A schema-valid but structurally incomplete on_off block — empty, or missing
        the selected mode's field — fails validate_config both on a device entry and on
        a groups[] entry, so load_config rejects the file instead of the service
        crashing later in bus_from_json. The identical block on a ``dali2: true`` entry
        is skipped by the loader and passes validation."""
        for malformed in ({}, {"on_action": {"mode": "scene"}, "off_action": {"mode": "off"}}):
            with self.subTest(device_on_off=malformed):
                config = _config(device_on_off=malformed)
                _validate(config)  # schema-valid on purpose
                with self.assertRaisesRegex(ValueError, "Invalid on_off block at gateway 'gw'"):
                    validate_config(config)
            with self.subTest(group_on_off=malformed):
                config = _config(groups=[{"number": 3, "on_off": malformed}])
                _validate(config)
                with self.assertRaisesRegex(ValueError, "Invalid on_off block at group 3"):
                    validate_config(config)
            with self.subTest(dali2_on_off=malformed):
                validate_config(_config(device_on_off=malformed, dali2=True))


class OnOffPersistenceTest(unittest.IsolatedAsyncioTestCase):

    async def test_on_off_config_survives_save_configuration(self):
        """A bus loaded from JSON with a device on_off block and a groups list writes
        both back verbatim in save_configuration."""
        bus = bus_from_json(
            "gw1",
            1,
            {
                "devices": [{"short": 5, "random": 123, "on_off": VALID_ON_OFF}],
                "groups": [{"number": 3, "on_off": VALID_ON_OFF}],
            },
            MagicMock(),
            MagicMock(),
        )
        gateway = WbDaliGateway(uid="gw1", buses=[bus])

        written = _save_to_temp([gateway])

        bus_entry = written["gateways"][0]["buses"][0]
        self.assertEqual(bus_entry["devices"][0]["on_off"], VALID_ON_OFF)
        self.assertEqual(bus_entry["groups"], [{"number": 3, "on_off": VALID_ON_OFF}])

    async def test_on_off_ignored_for_dali2_device(self):
        """An on_off block on a ``dali2: true`` entry passes the schema and is silently
        skipped: the config loads, no control appears and the block is not written back."""
        _validate(_config(device_on_off=VALID_ON_OFF, dali2=True))
        bus = bus_from_json(
            "gw1",
            1,
            {"devices": [{"short": 5, "random": 123, "dali2": True, "on_off": VALID_ON_OFF}]},
            MagicMock(),
            MagicMock(),
        )
        self.assertEqual(len(bus.dali2_devices), 1)
        self.assertIsNone(bus.dali2_devices[0].get_mqtt_control(ON_OFF))

        written = _save_to_temp([WbDaliGateway(uid="gw1", buses=[bus])])
        self.assertNotIn("on_off", written["gateways"][0]["buses"][0]["devices"][0])

    async def test_vanished_group_settings_dropped_on_save(self):
        # pylint: disable=protected-access
        """After a scan whose result leaves group 5 with no members (all devices
        initialized, so the membership is authoritative), the group's ``groups`` entry
        is dropped with a log line while the still-populated group 3 survives."""
        bus = bus_from_json(
            "gw1",
            1,
            {
                "devices": [],
                "groups": [
                    {"number": 3, "on_off": VALID_ON_OFF},
                    {"number": 5, "on_off": VALID_ON_OFF},
                ],
            },
            MagicMock(),
            MagicMock(),
        )
        bus._device_publisher = AsyncMock()
        member = _GearStub(1, groups={3})
        bus.dali_devices = [member]

        with self.assertLogs(bus.logger, level="INFO") as logs:
            await bus._apply_commissioning_results(CommissioningResult(unchanged=[member.address]), None)
        self.assertTrue(any("vanished group 5" in line for line in logs.output))

        self.assertEqual(set(bus.group_on_off), {3})
        written = _save_to_temp([WbDaliGateway(uid="gw1", buses=[bus])])
        self.assertEqual(
            written["gateways"][0]["buses"][0]["groups"],
            [{"number": 3, "on_off": VALID_ON_OFF}],
        )

    async def test_vanished_group_prune_requires_initialized_members(self):
        # pylint: disable=protected-access
        """With an uninitialized device on the bus its group membership is unknown, so
        no ``groups`` entry is pruned after a scan — even one with no known members."""
        bus = bus_from_json(
            "gw1",
            1,
            {
                "devices": [],
                "groups": [
                    {"number": 3, "on_off": VALID_ON_OFF},
                    {"number": 5, "on_off": VALID_ON_OFF},
                ],
            },
            MagicMock(),
            MagicMock(),
        )
        bus._device_publisher = AsyncMock()
        member = _GearStub(1, groups={3})
        pending = _GearStub(2, groups=(), is_initialized=False)
        bus.dali_devices = [member, pending]

        await bus._apply_commissioning_results(
            CommissioningResult(unchanged=[member.address, pending.address]), None
        )

        self.assertEqual(set(bus.group_on_off), {3, 5})
        written = _save_to_temp([WbDaliGateway(uid="gw1", buses=[bus])])
        self.assertEqual(len(written["gateways"][0]["buses"][0]["groups"]), 2)


class OnOffDeviceEditorTest(unittest.IsolatedAsyncioTestCase):

    async def test_device_on_off_editor_add_change_remove(self):
        # pylint: disable=protected-access
        """SetDevice add / no-op / change / remove flow for the device on_off block.

        A real DaliDevice is initialized through a scripted driver and served by a
        loop controller inside a Gateway. ``enabled: true`` adds and changes the block
        (the control is rebuilt without restart, the config is persisted, foreign-mode
        residue is dropped from device.params); rewriting the same block is a no-op
        (no rebuild, no save); ``enabled: false`` removes the block and the control
        without validating the rest of its content. The unconfigured GetDevice
        representation is exactly ``{"enabled": false}``.
        """
        param = OnOffSettingsParam()
        self.assertEqual(await param.read(AsyncMock(), GearShort(1)), {"on_off": {"enabled": False}})

        driver = ScriptedDriver()
        device = DaliDevice(DaliDeviceAddress(5, 0x123456), "gw_bus_1", MagicMock())
        await device.initialize(driver)
        device.params = {"short_address": 5}
        device.schema = {"type": "object"}
        merge_json_schemas(device.schema, on_off_editor_schema())

        controller = make_loop_controller()
        controller._dev = driver
        controller.dali_devices = [device]
        controller._devices_by_mqtt_id = {device.mqtt_id: device}
        controller._broadcast_device = BroadcastVirtualDevice(AggregatedCapabilities(), "gw_bus_1", "Bus 1")

        with patch("wb.mqtt_dali.gateway.save_configuration") as save_mock:
            gateway = Gateway(
                config={},
                mqtt_dispatcher=MagicMock(),
                config_path="",
                gtin_db=MagicMock(),
                command_registry={},
            )
            gateway.wb_dali_gateways = [WbDaliGateway(uid="gw", buses=[controller])]
            loop_task = asyncio.create_task(controller._polling_loop())
            try:
                response = await gateway.set_device_rpc_handler(
                    {"deviceId": device.uid, "config": {"on_off": {"enabled": True, **VALID_ON_OFF}}}
                )
                self.assertEqual(response["on_off"], {"enabled": True, **VALID_ON_OFF})
                control = device.get_mqtt_control(ON_OFF)
                self.assertIsInstance(control, OnOffControl)
                self.assertEqual(save_mock.call_count, 1)

                await gateway.set_device_rpc_handler(
                    {"deviceId": device.uid, "config": {"on_off": {"enabled": True, **VALID_ON_OFF}}}
                )
                self.assertEqual(save_mock.call_count, 1)
                self.assertIs(device.get_mqtt_control(ON_OFF), control)

                # Change mode; the foreign-mode residue ("percent") is ignored and does
                # not leak into the canonical params entry.
                residue_block = {
                    "enabled": True,
                    "on_action": {"mode": "scene", "scene": 2, "percent": 50},
                    "off_action": {"mode": "off"},
                }
                response = await gateway.set_device_rpc_handler(
                    {"deviceId": device.uid, "config": {"on_off": residue_block}}
                )
                self.assertEqual(response["on_off"], {"enabled": True, **SCENE_ON_OFF})
                self.assertEqual(save_mock.call_count, 2)

                # Remove: enabled false wins, the rest of the block is not validated
                # ("off" is not a valid on_action mode).
                response = await gateway.set_device_rpc_handler(
                    {
                        "deviceId": device.uid,
                        "config": {"on_off": {"enabled": False, "on_action": {"mode": "off"}}},
                    }
                )
                self.assertEqual(response["on_off"], {"enabled": False})
                self.assertIsNone(device.get_mqtt_control(ON_OFF))
                self.assertIsNone(device.on_off_config)
                self.assertEqual(save_mock.call_count, 3)
            finally:
                await stop_loop(controller, loop_task)

    async def test_device_schema_generation_exposes_on_off_block(self):
        """The device's generated GetDevice schema carries the on_off block through the
        real path: OnOffSettingsParam is one of the device's parameter handlers, so
        load_info merges its editor schema in (rather than the block being hand-merged)."""
        with patch.object(DaliDeviceBase, "_common_schema", {"type": "object", "properties": {}}):
            device = DaliDevice(DaliDeviceAddress(6, 0x654321), "gw_bus_1", MagicMock())
            driver = ScriptedDriver()
            await device.initialize(driver)
            await device.load_info(driver, force_reload=True)
        self.assertIn("on_off", device.schema["properties"])


class OnOffGroupEditorTest(unittest.IsolatedAsyncioTestCase):

    async def test_group_on_off_editor_roundtrip(self):
        # pylint: disable=protected-access
        """SetGroup/GetGroup roundtrip of the group on_off block.

        GetGroup answers in the GetDevice shape: config always carries the block with
        ``enabled`` and schema is the merged group schema. SetGroup with
        ``enabled: true`` stores the block and persists it into ``bus.groups`` without
        the ``enabled`` field; the same block again saves nothing; ``enabled: false``
        deletes the entry without validating the rest of the block.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "wb-mqtt-dali.conf")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("{}")
            config = {
                "gateways": [
                    {
                        "device_id": "gw1",
                        "buses": [{"devices": [], "groups": [{"number": 3, "on_off": SCENE_ON_OFF}]}],
                    }
                ]
            }
            gateway = Gateway(
                config=config,
                mqtt_dispatcher=MagicMock(),
                config_path=config_path,
                gtin_db=MagicMock(),
                command_registry={},
            )
            bus = gateway.wb_dali_gateways[0].buses[0]
            bus._state = ApplicationControllerState.READY
            bus._device_publisher = AsyncMock()
            loop_task = asyncio.create_task(bus._polling_loop())
            try:
                result = await gateway.get_group_rpc_handler({"groupId": f"{bus.uid}_g3"})
                self.assertEqual(result["config"]["on_off"], {"enabled": True, **SCENE_ON_OFF})
                self.assertIn("on_off", result["schema"]["properties"])

                result = await gateway.get_group_rpc_handler({"groupId": f"{bus.uid}_g5"})
                self.assertEqual(result["config"]["on_off"], {"enabled": False})

                await gateway.set_group_rpc_handler(
                    {"groupId": f"{bus.uid}_g3", "config": {"on_off": {"enabled": True, **VALID_ON_OFF}}}
                )
                with open(config_path, "r", encoding="utf-8") as f:
                    written = json.load(f)
                self.assertEqual(
                    written["gateways"][0]["buses"][0]["groups"],
                    [{"number": 3, "on_off": VALID_ON_OFF}],
                )

                # Rewriting the same block is a no-op: the deleted file is not recreated.
                os.unlink(config_path)
                await gateway.set_group_rpc_handler(
                    {"groupId": f"{bus.uid}_g3", "config": {"on_off": {"enabled": True, **VALID_ON_OFF}}}
                )
                self.assertFalse(os.path.exists(config_path))

                # enabled: false deletes the entry; the invalid remainder is ignored.
                await gateway.set_group_rpc_handler(
                    {
                        "groupId": f"{bus.uid}_g3",
                        "config": {"on_off": {"enabled": False, "off_action": {"mode": "level"}}},
                    }
                )
                with open(config_path, "r", encoding="utf-8") as f:
                    written = json.load(f)
                self.assertNotIn("groups", written["gateways"][0]["buses"][0])
                result = await gateway.get_group_rpc_handler({"groupId": f"{bus.uid}_g3"})
                self.assertEqual(result["config"]["on_off"], {"enabled": False})
            finally:
                await stop_loop(bus, loop_task)

    async def test_set_group_combined_params_and_on_off(self):
        # pylint: disable=protected-access
        """SetGroup carrying ballast parameters together with the on_off block.

        With ``apply_group_parameters`` mocked on a live bus: a combined call passes
        it the ballast parameters with on_off already stripped, and only after it
        succeeds is the block stored and persisted into ``bus.groups``. When it
        raises, the error propagates from SetGroup and the block is neither applied
        nor saved (the deleted config file is not recreated).
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "wb-mqtt-dali.conf")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("{}")
            gateway = Gateway(
                config={"gateways": [{"device_id": "gw1", "buses": [{"devices": []}]}]},
                mqtt_dispatcher=MagicMock(),
                config_path=config_path,
                gtin_db=MagicMock(),
                command_registry={},
            )
            bus = gateway.wb_dali_gateways[0].buses[0]
            bus._state = ApplicationControllerState.READY
            bus._device_publisher = AsyncMock()
            bus.apply_group_parameters = AsyncMock()
            loop_task = asyncio.create_task(bus._polling_loop())
            try:
                await gateway.set_group_rpc_handler(
                    {
                        "groupId": f"{bus.uid}_g3",
                        "config": {"min_level": 10, "on_off": {"enabled": True, **VALID_ON_OFF}},
                    }
                )
                bus.apply_group_parameters.assert_awaited_once_with(3, {"min_level": 10})
                self.assertEqual(bus.group_on_off, {3: on_off_config_from_json(VALID_ON_OFF)})
                with open(config_path, "r", encoding="utf-8") as f:
                    written = json.load(f)
                self.assertEqual(
                    written["gateways"][0]["buses"][0]["groups"],
                    [{"number": 3, "on_off": VALID_ON_OFF}],
                )

                bus.apply_group_parameters.side_effect = RuntimeError("group write failed")
                os.unlink(config_path)
                with self.assertRaisesRegex(RuntimeError, "group write failed"):
                    await gateway.set_group_rpc_handler(
                        {
                            "groupId": f"{bus.uid}_g5",
                            "config": {"min_level": 20, "on_off": {"enabled": True, **SCENE_ON_OFF}},
                        }
                    )
                self.assertEqual(set(bus.group_on_off), {3})
                self.assertFalse(os.path.exists(config_path))
            finally:
                await stop_loop(bus, loop_task)

    async def test_set_group_on_off_out_of_range_rejected(self):
        # pylint: disable=protected-access
        """SetGroup with an enabled on_off block carrying an out-of-range field value
        (percent > 100) is stopped by the editor-schema range gate: ValidationError
        propagates from the handler, the block is not applied to the bus and nothing
        is saved to the config file."""
        with patch("wb.mqtt_dali.gateway.save_configuration") as save_mock:
            gateway = Gateway(
                config={"gateways": [{"device_id": "gw1", "buses": [{"devices": []}]}]},
                mqtt_dispatcher=MagicMock(),
                config_path="",
                gtin_db=MagicMock(),
                command_registry={},
            )
            bus = gateway.wb_dali_gateways[0].buses[0]
            bus._state = ApplicationControllerState.READY
            bus._device_publisher = AsyncMock()
            loop_task = asyncio.create_task(bus._polling_loop())
            try:
                block = {
                    "enabled": True,
                    "on_action": {"mode": "level", "percent": 101, "fade_time": 3},
                    "off_action": {"mode": "off"},
                }
                with self.assertRaises(jsonschema.ValidationError):
                    await gateway.set_group_rpc_handler(
                        {"groupId": f"{bus.uid}_g3", "config": {"on_off": block}}
                    )
                self.assertEqual(bus.group_on_off, {})
                save_mock.assert_not_called()
            finally:
                await stop_loop(bus, loop_task)

    async def test_editor_block_requires_boolean_enabled(self):
        """An Editor-RPC on_off block with a missing or non-boolean ``enabled`` field
        is rejected with an error before any bus interaction or save; the group's
        stored settings stay untouched."""
        gateway = Gateway(
            config={"gateways": [{"device_id": "gw1", "buses": [{"devices": []}]}]},
            mqtt_dispatcher=MagicMock(),
            config_path="",
            gtin_db=MagicMock(),
            command_registry={},
        )
        bus = gateway.wb_dali_gateways[0].buses[0]
        for block in (
            dict(VALID_ON_OFF),
            {"enabled": 1, **VALID_ON_OFF},
            {"enabled": "true", **VALID_ON_OFF},
        ):
            with self.subTest(block=block):
                with self.assertRaisesRegex(ValueError, "enabled must be a boolean"):
                    await gateway.set_group_rpc_handler(
                        {"groupId": f"{bus.uid}_g3", "config": {"on_off": block}}
                    )
        self.assertEqual(bus.group_on_off, {})


if __name__ == "__main__":
    unittest.main()
