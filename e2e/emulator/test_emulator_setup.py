import shutil
import tempfile
import unittest
from pathlib import Path

from e2e.emulator.emulator_setup import ConfigBackup, build_serial_config
from e2e.emulator.installation_profile import profile_from_configs

PORT = {
    "path": "/dev/ttyRS485-1",
    "baud_rate": 9600,
    "parity": "E",
    "stop_bits": 1,
    "devices": [{"device_type": "WB-DALI", "slave_id": "5"}],
}
DALI_CONFIG = {"gateways": [{"device_id": "wb-dali_5", "buses": [{"devices": [{"short": 3, "random": 30}]}]}]}
DEVICES_STATE = {"1": {"gear": {"3": {"model": "default_gear"}}}}


class SerialConfigTest(unittest.TestCase):
    def test_serial_config_section_is_kept_but_for_the_path(self):
        """Only the gateway's port is installed, unchanged but for the pty path."""
        other = {"path": "/dev/ttyRS485-2", "devices": [{"device_type": "WB-MSW v.4", "slave_id": "11"}]}
        profile = profile_from_configs({"ports": [other, PORT]}, DALI_CONFIG, DEVICES_STATE)
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
            ["serial.conf", "serial.conf.e2e_emulator.bak"],
        )
        self.existing.write_text("replaced", encoding="utf-8")
        self.missing.write_text("replaced", encoding="utf-8")
        backup.restore()
        self.assertEqual(self.existing.read_text(encoding="utf-8"), "original")
        self.assertFalse(self.missing.exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["serial.conf"])

    def test_leftover_backup_is_refused_and_the_file_untouched(self):
        """A backup left by an interrupted run stops the take; neither file changes."""
        leftover = self.dir / "serial.conf.e2e_emulator.bak"
        leftover.write_text("older original", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            ConfigBackup([self.existing]).take()
        self.assertEqual(self.existing.read_text(encoding="utf-8"), "original")
        self.assertEqual(leftover.read_text(encoding="utf-8"), "older original")
