import json
import tempfile
import unittest
from pathlib import Path

from e2e.emulator.installation_profile import (
    DALI_CONFIG_FILE,
    DEVICES_STATE_FILE,
    KNOWN_GEAR_PRODUCTS,
    SERIAL_CONFIG_FILE,
    GearModel,
    Parity,
    load_profile,
    profile_from_configs,
    resolve_profile_path,
)

GATEWAY_PORT = {
    "path": "/dev/ttyRS485-2",
    "baud_rate": 9600,
    "parity": "E",
    "stop_bits": 1,
    "devices": [
        {"device_type": "WB-MSW v.4", "slave_id": "11"},
        {"device_type": "WB-DALI", "slave_id": "5"},
        {"device_type": "WB-MR6C v.3", "slave_id": "32", "guard_interval_us": 20000},
    ],
}
SERIAL_CONFIG = {
    "ports": [
        {"path": "/dev/ttyRS485-1", "devices": [{"device_type": "WB-LED-since-fw-3.8", "slave_id": "7"}]},
        GATEWAY_PORT,
    ]
}
DALI_CONFIG = {
    "gateways": [
        {
            "device_id": "wb-dali_5",
            "buses": [
                {
                    "devices": [
                        {"short": 0, "random": 10, "name": "OTI DALI 15/220-240/1A0 NFC 0"},
                        {"short": 4, "random": 11},
                        {"short": 6, "random": 12, "name": "BK-DCL010S-AHA0700ADN 6"},
                        {"short": 2, "random": 13, "dali2": True},
                        {"short": 3, "random": 14, "dali2": True},
                    ]
                },
                {"devices": []},
            ],
        }
    ]
}


OTI_NAME = "OTI DALI 15/220-240/1A0 NFC"
BK_NAME = "BK-DCL010S-AHA0700ADN"
GEAR_STATE = {"0": {"model": OTI_NAME}, "4": {"model": "default_gear"}, "6": {"model": BK_NAME}}
DEVICES_STATE = {"1": {"gear": GEAR_STATE}}


def profile(devices_state: dict):
    return profile_from_configs(SERIAL_CONFIG, DALI_CONFIG, devices_state)


class SerialConfigTest(unittest.TestCase):
    def test_gateway_port_gives_line_settings_and_neighbours(self):
        """Of two ports the one with the WB-DALI is taken: its line settings, slave id and section.
        Every other device of that port is a neighbour, other ports are ignored."""
        result = profile(DEVICES_STATE)
        self.assertEqual(
            (result.port.baud_rate, result.port.parity, result.port.stop_bits), (9600, Parity.EVEN, 1)
        )
        self.assertEqual((result.gateway_slave_id, result.gateway_device_id), (5, "wb-dali_5"))
        self.assertEqual(
            [(n.template, n.slave_id) for n in result.neighbours], [("WB-MSW v.4", 11), ("WB-MR6C v.3", 32)]
        )
        self.assertEqual(result.serial_config, GATEWAY_PORT)

    def test_exactly_one_gateway_is_required(self):
        """A config without a WB-DALI or with two of them is rejected."""
        no_gateway = {"ports": [SERIAL_CONFIG["ports"][0]]}
        two_gateways = {"ports": [GATEWAY_PORT, GATEWAY_PORT]}
        for serial_config in (no_gateway, two_gateways):
            with self.subTest(serial_config=serial_config), self.assertRaises(ValueError):
                profile_from_configs(serial_config, DALI_CONFIG, DEVICES_STATE)


class DaliConfigTest(unittest.TestCase):
    def test_devices_and_models_come_from_the_config(self):
        """Gear models and GTINs follow `model` in devices-state, default_gear is plain without GTIN.
        `dali2` entries are control devices; random addresses are taken from the config."""
        bus = profile(DEVICES_STATE).buses[0]
        oti = KNOWN_GEAR_PRODUCTS[OTI_NAME]
        bk = KNOWN_GEAR_PRODUCTS[BK_NAME]
        self.assertEqual(
            [(g.address, g.random_address, g.model, g.gtin) for g in bus.gear],
            [(0, 10, oti.model, oti.gtin), (4, 11, GearModel.PLAIN, None), (6, 12, bk.model, bk.gtin)],
        )
        self.assertEqual([(d.address, d.random_address) for d in bus.control_devices], [(2, 13), (3, 14)])
        self.assertEqual(len(profile(DEVICES_STATE).buses), 2)

    def test_config_without_the_gateway_is_rejected(self):
        other = {"gateways": [{**DALI_CONFIG["gateways"][0], "device_id": "wb-dali_6"}]}
        with self.assertRaises(ValueError):
            profile_from_configs(SERIAL_CONFIG, other, DEVICES_STATE)


class DevicesStateTest(unittest.TestCase):
    def test_absent_devices_get_the_defaults(self):
        """Gear absent from devices-state has no groups, a control device absent has instance type 1."""
        gear = {**GEAR_STATE, "0": {"model": OTI_NAME, "groups": [3]}}
        bus = profile({"1": {"gear": gear, "control_devices": {"2": {"instance_types": [1, 4]}}}}).buses[0]
        self.assertEqual([g.groups for g in bus.gear], [frozenset({3}), frozenset(), frozenset()])
        self.assertEqual([d.instance_types for d in bus.control_devices], [(1, 4), (1,)])

    def test_device_missing_from_the_config_is_rejected(self):
        """An entry for an unknown short address, a device of the other kind or an unknown bus fails."""
        for state in (
            {"1": {"gear": {**GEAR_STATE, "9": {"model": OTI_NAME}}}},
            {"1": {"gear": {**GEAR_STATE, "2": {"model": OTI_NAME}}}},
            {"1": {"gear": GEAR_STATE, "control_devices": {"0": {}}}},
            {**DEVICES_STATE, "3": {}},
        ):
            with self.subTest(state=state), self.assertRaises(ValueError):
                profile(state)

    def test_malformed_entries_are_rejected(self):
        """A gear without an entry or without a `model`, an unknown model, a list instead of an object,
        an unknown key or a key of the other kind."""
        for state in (
            {"1": {"gear": {"0": GEAR_STATE["0"], "6": GEAR_STATE["6"]}}},
            {"1": {"gear": {**GEAR_STATE, "4": {"groups": [1]}}}},
            {"1": {"gear": {**GEAR_STATE, "4": {"model": "plain"}}}},
            {"1": {"gear": {**GEAR_STATE, "4": [3]}}},
            {"1": {"gear": GEAR_STATE, "control_devices": {"2": [1]}}},
            {"1": {"gear": {**GEAR_STATE, "4": {"model": OTI_NAME, "modle": OTI_NAME}}}},
            {"1": {"gear": {**GEAR_STATE, "4": {"model": OTI_NAME, "instance_types": [1]}}}},
            {"1": {"gear": GEAR_STATE, "control_devices": {"2": {"groups": [1]}}}},
        ):
            with self.subTest(state=state), self.assertRaises(ValueError):
                profile(state)


class ResolveProfileTest(unittest.TestCase):
    def test_bare_name_is_a_profile_of_the_e2e(self):
        profiles = Path("/e2e/profiles")
        self.assertEqual(resolve_profile_path("site", profiles), profiles / "site")
        self.assertEqual(resolve_profile_path("./site", profiles), Path("site"))
        self.assertEqual(resolve_profile_path("/tmp/site", profiles), Path("/tmp/site"))

    def test_directory_path_is_loaded(self):
        """A directory with the three files loads as a profile."""
        with tempfile.TemporaryDirectory() as tmp:
            for name, data in (
                (SERIAL_CONFIG_FILE, SERIAL_CONFIG),
                (DALI_CONFIG_FILE, DALI_CONFIG),
                (
                    DEVICES_STATE_FILE,
                    {"1": {"gear": {**GEAR_STATE, "4": {"model": "default_gear", "groups": [7]}}}},
                ),
            ):
                Path(tmp, name).write_text(json.dumps(data), encoding="utf-8")
            result = load_profile(Path(tmp))
        self.assertEqual(result.buses[0].gear[1].groups, frozenset({7}))
