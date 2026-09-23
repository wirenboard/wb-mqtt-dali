"""Config-side and Editor-RPC behaviour of the ``on_off`` feature.

Covers the validation split (ranges by JSON schema, requiredness by the parser),
round-trip through ``save_configuration``, the post-scan pruning of vanished groups'
settings, the silent skip on dali2 entries, duplicate group numbers, and the
SetDevice/SetGroup/GetGroup editor flows with the ``enabled`` RPC representation.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jsonschema
from dali.address import GearGroup, GearShort
from dali.gear.general import DTR0, SetMinLevel

from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.config_validator import validate_config
from wb.mqtt_dali.control_ids import ON_OFF
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.gateway import (
    Gateway,
    WbDaliGateway,
    bus_from_json,
    bus_to_json,
    save_configuration,
)
from wb.mqtt_dali.on_off_control import (
    FADE_TIME_USE_DEVICE,
    OnOffControl,
    OnOffSettingsParam,
    on_off_config_from_json,
    on_off_config_to_json,
    on_off_editor_schema,
)

from ._on_off_helpers import (
    BusScript,
    ScriptedBus,
    ScriptedDriver,
    frames,
    group_settings,
)

DaliDeviceBase._common_schema = {"title": "test-schema"}  # pylint: disable=protected-access


def _schema_path() -> Path:
    # Under pybuild the tests run from a build copy, the schema stays in the source root above it.
    for directory in Path(__file__).resolve().parents:
        candidate = directory / "wb-mqtt-dali.schema.json"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("wb-mqtt-dali.schema.json not found above " + __file__)


_SCHEMA = json.loads(_schema_path().read_text(encoding="utf-8"))

VALID_ON_OFF = {
    "on_action": {"mode": "level", "percent": 50, "fade_time": 3},
    "off_action": {"mode": "dapc", "fade_time": 1},
}
SCENE_ON_OFF = {
    "on_action": {"mode": "scene", "scene": 2},
    "off_action": {"mode": "off"},
}

NO_GROUPS = [False] * 16


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


def _save_to_temp(gateways) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wb-mqtt-dali.conf")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        save_configuration(path, debug=False, gateways=gateways)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def _bus_config(shorts, group_numbers) -> dict:
    return {
        "devices": [{"short": short, "random": 0xABCDEF + short} for short in shorts],
        "groups": [{"number": number, "on_off": VALID_ON_OFF} for number in group_numbers],
    }


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

    def test_fade_time_use_device_sentinel_drops_the_key(self):
        """The editor's ``fade_time == -1`` parses to no fade time, so the canonical config
        omits the key for every fade-capable mode; a real 0..15 code is preserved."""
        for on_action in (
            {"mode": "level", "percent": 50, "fade_time": FADE_TIME_USE_DEVICE},
            {"mode": "dapc", "value": 200, "fade_time": FADE_TIME_USE_DEVICE},
            {"mode": "last_active_level", "fade_time": FADE_TIME_USE_DEVICE},
        ):
            with self.subTest(on_action=on_action):
                config = on_off_config_from_json(
                    {
                        "on_action": on_action,
                        "off_action": {"mode": "dapc", "fade_time": FADE_TIME_USE_DEVICE},
                    }
                )
                canonical = on_off_config_to_json(config)
                self.assertNotIn("fade_time", canonical["on_action"])
                self.assertNotIn("fade_time", canonical["off_action"])
        preserved = on_off_config_from_json(
            {"on_action": {"mode": "level", "percent": 50, "fade_time": 3}, "off_action": {"mode": "off"}}
        )
        self.assertEqual(on_off_config_to_json(preserved)["on_action"]["fade_time"], 3)

    def test_fade_time_schema_offers_use_device_option(self):
        """The editor fade_time field carries the -1 sentinel as its default and its first
        enum entry, titled "use device settings", so the value never reaches the config file."""
        fade = on_off_editor_schema()["properties"]["on_off"]["properties"]["on_action"]["properties"][
            "fade_time"
        ]
        self.assertEqual(fade["default"], FADE_TIME_USE_DEVICE)
        self.assertEqual(fade["enum"][0], FADE_TIME_USE_DEVICE)
        self.assertEqual(fade["options"]["enum_titles"][0], "use device settings")

    def test_group_fade_time_hint_in_editor_schema(self):
        """The group editor schema states that a group fade time is not restored; the
        device schema carries no such note."""
        group_fade = on_off_editor_schema(group_and_broadcast=True)["properties"]["on_off"]["properties"][
            "on_action"
        ]["properties"]["fade_time"]
        self.assertIn("not restored", group_fade["description"])
        self.assertIn(group_fade["description"], on_off_editor_schema(True)["translations"]["ru"])
        device_fade = on_off_editor_schema()["properties"]["on_off"]["properties"]["on_action"]["properties"][
            "fade_time"
        ]
        self.assertNotIn("description", device_fade)


class OnOffConfigValidatorTest(unittest.TestCase):

    def test_duplicate_group_numbers_rejected(self):
        groups = [{"number": 3, "on_off": VALID_ON_OFF}, {"number": 3, "on_off": SCENE_ON_OFF}]
        with self.assertRaisesRegex(ValueError, "Duplicate group 3"):
            validate_config(_config(groups=groups))
        validate_config(_config(groups=[{"number": 3, "on_off": VALID_ON_OFF}]))

    def test_malformed_on_off_rejected_at_config_load(self):
        """A schema-valid but structurally incomplete on_off block fails validate_config on a
        device entry, on a groups[] entry and on a ``dali2: true`` one, so load_config rejects it."""
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
                with self.assertRaisesRegex(ValueError, "Invalid on_off block at gateway 'gw'"):
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

    async def test_unreported_group_keeps_its_settings_on_save(self):
        """The only gear on the bus reports group 3 and not group 5: the on/off setting keeps
        group 5 in the rewritten config all the same."""
        async with ScriptedBus(BusScript(groups={1: {3}})) as harness:
            bus = await harness.start_bus(_bus_config(shorts=[1], group_numbers=[3, 5]))
            await harness.wait_until(lambda: bus.dali_devices[0].is_initialized)
            await harness.settle()

            self.assertEqual(set(group_settings(bus)), {3, 5})
            written = _save_to_temp([WbDaliGateway(uid="gw1", buses=[bus])])
            self.assertEqual(
                written["gateways"][0]["buses"][0]["groups"],
                [{"number": 3, "on_off": VALID_ON_OFF}, {"number": 5, "on_off": VALID_ON_OFF}],
            )

    async def test_configured_group_survives_its_last_member_leaving(self):
        """Clearing the last member's group membership through SetDevice leaves the configured
        group published with its settings: an enabled on/off block keeps a group alive."""
        config = {"gateways": [{"device_id": "gw1", "buses": [_bus_config(shorts=[5], group_numbers=[3])]}]}
        async with ScriptedBus(BusScript(groups={5: {3}})) as harness:
            with patch("wb.mqtt_dali.gateway.save_configuration") as save_mock:
                gateway = Gateway(
                    config=config,
                    mqtt_dispatcher=harness.dispatcher,
                    config_path="",
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])
                member = bus.dali_devices[0]
                await harness.wait_until(lambda: member.is_initialized)
                await harness.settle()
                group_mqtt_id = bus.group_devices[3].mqtt_id
                settings = await gateway.get_device_rpc_handler({"deviceId": member.uid})

                await gateway.set_device_rpc_handler(
                    {"deviceId": member.uid, "config": {**settings["config"], "groups": NO_GROUPS}}
                )
                await harness.settle()

                self.assertEqual(group_settings(bus), {3: on_off_config_from_json(VALID_ON_OFF)})
                self.assertIn(3, bus.group_devices)
                self.assertIn(group_mqtt_id, harness.published_devices())
                self.assertEqual(save_mock.call_count, 1)

    async def test_unconfigured_group_goes_away_with_its_last_member(self):
        """A group published only because a ballast reported it has no settings to keep it:
        SetDevice clearing that membership unpublishes the group."""
        config = {"gateways": [{"device_id": "gw1", "buses": [_bus_config(shorts=[5], group_numbers=[])]}]}
        async with ScriptedBus(BusScript(groups={5: {3}})) as harness:
            with patch("wb.mqtt_dali.gateway.save_configuration"):
                gateway = Gateway(
                    config=config,
                    mqtt_dispatcher=harness.dispatcher,
                    config_path="",
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])
                member = bus.dali_devices[0]
                await harness.wait_until(lambda: member.is_initialized)
                await harness.settle()
                group_mqtt_id = bus.group_devices[3].mqtt_id
                settings = await gateway.get_device_rpc_handler({"deviceId": member.uid})

                await gateway.set_device_rpc_handler(
                    {"deviceId": member.uid, "config": {**settings["config"], "groups": NO_GROUPS}}
                )
                await harness.settle()

                self.assertNotIn(3, bus.group_devices)
                self.assertNotIn(group_mqtt_id, harness.published_devices())


class OnOffDeviceEditorTest(unittest.IsolatedAsyncioTestCase):

    async def test_device_on_off_editor_add_change_remove(self):
        """SetDevice add / rewrite / change / remove flow for the device on_off block: the
        control follows without a restart, foreign-mode residue is dropped, the config is saved."""
        param = OnOffSettingsParam()
        self.assertEqual(await param.read(AsyncMock(), GearShort(1)), {"on_off": {"enabled": False}})

        config = {"gateways": [{"device_id": "gw1", "buses": [{"devices": [{"short": 5, "random": 1}]}]}]}
        async with ScriptedBus() as harness:
            with patch("wb.mqtt_dali.gateway.save_configuration") as save_mock:
                gateway = Gateway(
                    config=config,
                    mqtt_dispatcher=harness.dispatcher,
                    config_path="",
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])
                device = bus.dali_devices[0]
                await harness.wait_until(lambda: device.is_initialized)
                settings = await gateway.get_device_rpc_handler({"deviceId": device.uid})
                self.assertEqual(settings["config"]["on_off"], {"enabled": False})

                response = await gateway.set_device_rpc_handler(
                    {
                        "deviceId": device.uid,
                        "config": {**settings["config"], "on_off": {"enabled": True, **VALID_ON_OFF}},
                    }
                )
                self.assertEqual(response["on_off"], {"enabled": True, **VALID_ON_OFF})
                control = device.get_mqtt_control(ON_OFF)
                self.assertIsInstance(control, OnOffControl)
                self.assertEqual(save_mock.call_count, 1)

                await gateway.set_device_rpc_handler(
                    {
                        "deviceId": device.uid,
                        "config": {**settings["config"], "on_off": {"enabled": True, **VALID_ON_OFF}},
                    }
                )
                self.assertIs(device.get_mqtt_control(ON_OFF), control)

                # Change mode; the foreign-mode residue ("percent") is ignored and does
                # not leak into the canonical params entry.
                residue_block = {
                    "enabled": True,
                    "on_action": {"mode": "scene", "scene": 2, "percent": 50},
                    "off_action": {"mode": "off"},
                }
                response = await gateway.set_device_rpc_handler(
                    {"deviceId": device.uid, "config": {**settings["config"], "on_off": residue_block}}
                )
                self.assertEqual(response["on_off"], {"enabled": True, **SCENE_ON_OFF})
                self.assertEqual(save_mock.call_count, 3)

                response = await gateway.set_device_rpc_handler(
                    {"deviceId": device.uid, "config": {**settings["config"], "on_off": {"enabled": False}}}
                )
                self.assertEqual(response["on_off"], {"enabled": False})
                self.assertIsNone(device.get_mqtt_control(ON_OFF))
                self.assertIsNone(device.on_off_config)
                self.assertEqual(save_mock.call_count, 4)

    async def test_get_device_sends_use_device_fade_time_for_absent_key(self):
        """A config block without fade_time reads back as -1 in every fade-capable action;
        sending that answer back leaves the config and the control unchanged."""
        on_off = {"on_action": {"mode": "last_active_level"}, "off_action": {"mode": "dapc"}}
        config = {
            "gateways": [
                {"device_id": "gw1", "buses": [{"devices": [{"short": 5, "random": 1, "on_off": on_off}]}]}
            ]
        }
        async with ScriptedBus() as harness:
            with patch("wb.mqtt_dali.gateway.save_configuration"):
                gateway = Gateway(
                    config=config,
                    mqtt_dispatcher=harness.dispatcher,
                    config_path="",
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])
                device = bus.dali_devices[0]
                await harness.wait_until(lambda: device.is_initialized)
                original = device.on_off_config
                control = device.get_mqtt_control(ON_OFF)

                settings = await gateway.get_device_rpc_handler({"deviceId": device.uid})
                self.assertEqual(
                    settings["config"]["on_off"],
                    {
                        "enabled": True,
                        "on_action": {"mode": "last_active_level", "fade_time": FADE_TIME_USE_DEVICE},
                        "off_action": {"mode": "dapc", "fade_time": FADE_TIME_USE_DEVICE},
                    },
                )

                await gateway.set_device_rpc_handler({"deviceId": device.uid, "config": settings["config"]})
                self.assertEqual(device.on_off_config, original)
                self.assertIs(device.get_mqtt_control(ON_OFF), control)
                self.assertEqual(on_off_config_to_json(device.on_off_config), on_off)

    async def test_device_schema_generation_exposes_on_off_block(self):
        """OnOffSettingsParam is one of the device's parameter handlers, so load_info merges
        its editor schema into the GetDevice schema instead of the block being hand-merged."""
        with patch.object(DaliDeviceBase, "_common_schema", {"type": "object", "properties": {}}):
            device = DaliDevice(DaliDeviceAddress(6, 0x654321), "gw_bus_1", MagicMock())
            driver = ScriptedDriver()
            await device.initialize(driver)
            await device.load_info(driver, force_reload=True)
        self.assertIn("on_off", device.schema["properties"])


class OnOffGroupEditorTest(unittest.IsolatedAsyncioTestCase):

    async def test_bus_to_json_lists_group_ids(self):
        """The tree GetList serves lists every group of the bus - the one the gear reports and
        the configured memberless one - each with the id GetGroup takes."""
        async with ScriptedBus(BusScript(groups={1: {3}})) as harness:
            bus = await harness.start_bus(_bus_config(shorts=[1], group_numbers=[5]))
            await harness.wait_until(lambda: bus.dali_devices[0].is_initialized)
            await harness.settle()

            self.assertEqual(
                bus_to_json(bus)["groups"],
                [{"id": f"{bus.uid}_g3", "number": 3}, {"id": f"{bus.uid}_g5", "number": 5}],
            )

    async def test_group_on_off_editor_roundtrip(self):
        """GetGroup answers in the GetDevice shape; SetGroup with ``enabled: true`` persists the
        block into ``bus.groups``, and ``enabled: false`` on a memberless group deletes both."""
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
            async with ScriptedBus() as harness:
                gateway = Gateway(
                    config=config,
                    mqtt_dispatcher=harness.dispatcher,
                    config_path=config_path,
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])

                result = await gateway.get_group_rpc_handler({"groupId": f"{bus.uid}_g3"})
                self.assertEqual(result["config"]["on_off"], {"enabled": True, **SCENE_ON_OFF})
                self.assertIn("on_off", result["schema"]["properties"])

                with self.assertRaisesRegex(ValueError, "not found"):
                    await gateway.get_group_rpc_handler({"groupId": f"{bus.uid}_g5"})

                await gateway.set_group_rpc_handler(
                    {"groupId": f"{bus.uid}_g3", "config": {"on_off": {"enabled": True, **VALID_ON_OFF}}}
                )
                with open(config_path, "r", encoding="utf-8") as f:
                    written = json.load(f)
                self.assertEqual(
                    written["gateways"][0]["buses"][0]["groups"],
                    [{"number": 3, "on_off": VALID_ON_OFF}],
                )

                # Rewriting the same block leaves the file as it was.
                await gateway.set_group_rpc_handler(
                    {"groupId": f"{bus.uid}_g3", "config": {"on_off": {"enabled": True, **VALID_ON_OFF}}}
                )
                with open(config_path, "r", encoding="utf-8") as f:
                    self.assertEqual(json.load(f), written)

                # enabled: false deletes the entry, and with no member reporting the group,
                # the group itself: the empty config and schema say so.
                result = await gateway.set_group_rpc_handler(
                    {"groupId": f"{bus.uid}_g3", "config": {"on_off": {"enabled": False}}}
                )
                self.assertEqual(result, {"config": {}, "schema": {}})
                with open(config_path, "r", encoding="utf-8") as f:
                    written = json.load(f)
                self.assertNotIn("groups", written["gateways"][0]["buses"][0])
                with self.assertRaisesRegex(ValueError, "not found"):
                    await gateway.get_group_rpc_handler({"groupId": f"{bus.uid}_g3"})

    async def test_set_group_combined_params_and_on_off(self):
        """SetGroup with ballast parameters and the on_off block together: the parameters
        reach the members over the group address, and the block is saved only if they succeed."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "wb-mqtt-dali.conf")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("{}")
            script = BusScript(groups={1: {3, 5}}, dead_groups=frozenset({5}))
            async with ScriptedBus(script) as harness:
                gateway = Gateway(
                    config={
                        "gateways": [
                            {"device_id": "gw1", "buses": [{"devices": [{"short": 1, "random": 1}]}]}
                        ]
                    },
                    mqtt_dispatcher=harness.dispatcher,
                    config_path=config_path,
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])
                await harness.wait_until(lambda: bus.dali_devices[0].is_initialized)
                await harness.settle()
                harness.drain()

                await gateway.set_group_rpc_handler(
                    {
                        "groupId": f"{bus.uid}_g3",
                        "config": {"min_level": 10, "on_off": {"enabled": True, **VALID_ON_OFF}},
                    }
                )
                self.assertEqual(frames(harness.sent), frames([DTR0(10), SetMinLevel(GearGroup(3))]))
                self.assertEqual(group_settings(bus), {3: on_off_config_from_json(VALID_ON_OFF)})
                with open(config_path, "r", encoding="utf-8") as f:
                    written = json.load(f)
                self.assertEqual(
                    written["gateways"][0]["buses"][0]["groups"],
                    [{"number": 3, "on_off": VALID_ON_OFF}],
                )

                # Group 5's frames do not get through, so its write fails.
                os.unlink(config_path)
                with self.assertRaises(RuntimeError):
                    await gateway.set_group_rpc_handler(
                        {
                            "groupId": f"{bus.uid}_g5",
                            "config": {"min_level": 20, "on_off": {"enabled": True, **SCENE_ON_OFF}},
                        }
                    )
                self.assertEqual(set(group_settings(bus)), {3})
                self.assertFalse(os.path.exists(config_path))

    async def test_set_group_on_off_out_of_range_rejected(self):
        """An enabled block with an out-of-range field (percent > 100) is stopped by the editor
        schema: ValidationError propagates, nothing reaches the bus and nothing is saved."""
        async with ScriptedBus(BusScript(groups={1: {3}})) as harness:
            with patch("wb.mqtt_dali.gateway.save_configuration") as save_mock:
                gateway = Gateway(
                    config={
                        "gateways": [
                            {"device_id": "gw1", "buses": [{"devices": [{"short": 1, "random": 1}]}]}
                        ]
                    },
                    mqtt_dispatcher=harness.dispatcher,
                    config_path="",
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])
                await harness.wait_until(lambda: 3 in bus.group_devices)

                block = {
                    "enabled": True,
                    "on_action": {"mode": "level", "percent": 101, "fade_time": 3},
                    "off_action": {"mode": "off"},
                }
                with self.assertRaises(jsonschema.ValidationError):
                    await gateway.set_group_rpc_handler(
                        {"groupId": f"{bus.uid}_g3", "config": {"on_off": block}}
                    )
                self.assertEqual(group_settings(bus), {})
                save_mock.assert_not_called()

    async def test_editor_block_requires_boolean_enabled(self):
        """An Editor-RPC on_off block with a missing or non-boolean ``enabled`` field is
        rejected before anything reaches the bus or the config file."""
        async with ScriptedBus(BusScript(groups={1: {3}})) as harness:
            with patch("wb.mqtt_dali.gateway.save_configuration") as save_mock:
                gateway = Gateway(
                    config={
                        "gateways": [
                            {"device_id": "gw1", "buses": [{"devices": [{"short": 1, "random": 1}]}]}
                        ]
                    },
                    mqtt_dispatcher=harness.dispatcher,
                    config_path="",
                    gtin_db=MagicMock(),
                    command_registry={},
                )
                bus = await harness.start(gateway.wb_dali_gateways[0].buses[0])
                await harness.wait_until(lambda: 3 in bus.group_devices)

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
                self.assertEqual(group_settings(bus), {})
                save_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
