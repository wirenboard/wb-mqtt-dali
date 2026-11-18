import unittest

from e2e.emulator.installation_profile import GearModel, load_profile
from e2e.large_installation.e2e_large_installation import PROFILES_DIR


class ShippedProfilesTest(unittest.TestCase):
    def test_site_matches_the_object(self):
        """The site profile has the object's 2 / 28 / 25 gear, 14 neighbours, one group per gear."""
        result = load_profile(PROFILES_DIR / "site")
        self.assertEqual([len(b.gear) for b in result.buses], [2, 28, 25])
        self.assertEqual(len(result.neighbours), 14)
        self.assertTrue(all(len(g.groups) == 1 for b in result.buses for g in b.gear))

    def test_full_gateway_fills_every_address(self):
        """64 gear with 52 OTI and 64 control devices with instances 1-4 on each of 3 buses."""
        result = load_profile(PROFILES_DIR / "full_gateway")
        energy = [sum(g.model == GearModel.ENERGY for g in b.gear) for b in result.buses]
        self.assertEqual([len(b.gear) for b in result.buses], [64] * 3)
        self.assertEqual(energy, [52] * 3)
        self.assertEqual([len(b.control_devices) for b in result.buses], [64] * 3)
        self.assertTrue(
            all(d.instance_types == (1, 2, 3, 4) for b in result.buses for d in b.control_devices)
        )

    def test_site_slow_setup_guards_four_relays(self):
        """site-slow-setup is site with guard_interval_us 150000 on WB-MR6C v.3 32, 45, 51, 53."""
        result = load_profile(PROFILES_DIR / "site-slow-setup")
        guarded = {
            int(d["slave_id"]): d["device_type"]
            for d in result.serial_config["devices"]
            if d.get("guard_interval_us") == 150000
        }
        self.assertEqual(guarded, dict.fromkeys((32, 45, 51, 53), "WB-MR6C v.3"))
        self.assertEqual(sum("guard_interval_us" in d for d in result.serial_config["devices"]), len(guarded))
        self.assertEqual(result.buses, load_profile(PROFILES_DIR / "site").buses)

    def test_full_gateway_9600_is_full_gateway_on_9600(self):
        """Same devices as full_gateway; the port and every device are set to 9600."""
        result = load_profile(PROFILES_DIR / "full_gateway_9600")
        self.assertEqual(result.buses, load_profile(PROFILES_DIR / "full_gateway").buses)
        self.assertEqual(result.port.baud_rate, 9600)
        self.assertTrue(all(d.get("baud_rate", 96) == 96 for d in result.serial_config["devices"]))
