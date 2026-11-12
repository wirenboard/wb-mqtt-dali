import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from e2e.large_installation.installation_profile import (
    bus_device_ids,
    expected_devices,
    profile_from_configs,
)
from e2e.large_installation.stand import (
    ConfigBackup,
    MetaWatcher,
    build_dali_config,
    build_serial_config,
    full_start_time,
    restarted_services,
)

PORT = {
    "path": "/dev/ttyRS485-2",
    "baud_rate": 115200,
    "parity": "N",
    "stop_bits": 2,
    "devices": [{"device_type": "WB-DALI", "slave_id": "5", "baud_rate": 1152}],
}
DALI_CONFIG = {
    "debug": True,
    "gateways": [
        {
            "device_id": "wb-dali_5",
            "buses": [
                {"devices": [{"short": 3, "random": 30}, {"short": 4, "random": 40, "dali2": True}]},
                {"devices": [{"short": 0, "random": 50, "mqtt_id": "kitchen"}]},
            ],
        }
    ],
}


class StandConfigTest(unittest.TestCase):
    def test_expected_devices_follow_wb_mqtt_dali_naming(self):
        """Default names and ids for gear and DALI-2 devices; a custom mqtt_id is taken as is."""
        devices = expected_devices(DALI_CONFIG, "wb-dali_5")
        self.assertEqual(
            [(d.bus, d.short, d.dali2, d.mqtt_id, d.name) for d in devices],
            [
                (1, 3, False, "wb-dali_5_bus_1_3", "DALI 3"),
                (1, 4, True, "wb-dali_5_bus_1_dali2_4", "DALI-2 4"),
                (2, 0, False, "kitchen", "DALI 0"),
            ],
        )
        self.assertEqual(
            bus_device_ids(DALI_CONFIG, "wb-dali_5"),
            ["wb-dali_5_bus_1_broadcast", "wb-dali_5_bus_2_broadcast"],
        )


class DaliConfigDebugTest(unittest.TestCase):
    def test_debug_is_dropped_from_a_profile_dali_config(self):
        """The profile's wb-mqtt-dali config is installed as is but for `debug`."""
        profile = profile_from_configs({"ports": [PORT]}, DALI_CONFIG, {})
        self.assertEqual(build_dali_config(profile), {"gateways": DALI_CONFIG["gateways"]})
        self.assertTrue(profile.dali_config["debug"])


class SerialConfigTest(unittest.TestCase):
    def test_serial_config_section_is_kept_but_for_the_path(self):
        """Only the gateway's port is installed, unchanged but for the pty path."""
        other = {"path": "/dev/ttyRS485-1", "devices": [{"device_type": "WB-MSW v.4", "slave_id": "11"}]}
        profile = profile_from_configs({"ports": [other, PORT]}, DALI_CONFIG, {})
        config = build_serial_config(profile)
        self.assertEqual(len(config["ports"]), 1)
        port = config["ports"][0]
        self.assertEqual(
            {k: v for k, v in port.items() if k != "path"}, {k: v for k, v in PORT.items() if k != "path"}
        )
        self.assertNotEqual(port["path"], PORT["path"])


class ConfigBackupTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.existing = self.dir / "serial.conf"
        self.missing = self.dir / "dali.conf"
        self.existing.write_text("original", encoding="utf-8")

    def test_take_overwrite_and_restore(self):
        """The existing file comes back as it was, the missing one is removed again, the backups go."""
        backup = ConfigBackup([self.existing, self.missing])
        backup.take()
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["serial.conf", "serial.conf.e2e_large_installation.bak"],
        )
        self.existing.write_text("stand", encoding="utf-8")
        self.missing.write_text("stand", encoding="utf-8")
        backup.restore()
        self.assertEqual(self.existing.read_text(encoding="utf-8"), "original")
        self.assertFalse(self.missing.exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["serial.conf"])

    def test_leftover_backup_is_refused_and_the_file_untouched(self):
        """A backup left by an interrupted run stops the take; neither file changes."""
        leftover = self.dir / "serial.conf.e2e_large_installation.bak"
        leftover.write_text("older original", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            ConfigBackup([self.existing]).take()
        self.assertEqual(self.existing.read_text(encoding="utf-8"), "original")
        self.assertEqual(leftover.read_text(encoding="utf-8"), "older original")


@dataclass
class Message:
    topic: str
    payload: bytes
    retain: bool = False


class FullStartTest(unittest.TestCase):
    def test_only_the_first_live_meta_counts(self):
        """Retained and empty /meta are ignored; a repeated live one keeps the first time."""
        watcher = MetaWatcher()
        watcher.handle(Message("/devices/a/meta", b"{}", retain=True))
        watcher.handle(Message("/devices/b/meta", b""))
        watcher.handle(Message("/devices/a/meta", b"{}"))
        first = watcher.seen["a"]
        watcher.handle(Message("/devices/a/meta", b"{}"))
        self.assertEqual(watcher.seen, {"a": first})

    def test_full_start_is_the_last_of_the_wanted_devices(self):
        seen = {"a": 10.0, "b": 12.5, "other": 30.0}
        self.assertEqual(full_start_time(seen, {"a", "b"}), 12.5)
        self.assertIsNone(full_start_time(seen, {"a", "b", "c"}))


class ServiceRestartTest(unittest.TestCase):
    def test_a_higher_restart_count_is_reported(self):
        """A unit whose NRestarts grew is reported; an unchanged unit is not."""
        before = {"wb-mqtt-serial": 0, "wb-mqtt-dali": 0}
        self.assertEqual(restarted_services(before, dict(before)), [])
        self.assertEqual(
            restarted_services(before, {"wb-mqtt-serial": 1, "wb-mqtt-dali": 0}),
            ["wb-mqtt-serial restarted during the iteration: NRestarts 0 -> 1"],
        )
