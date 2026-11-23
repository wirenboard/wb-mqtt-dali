import unittest

from dali.address import GearShort
from dali.gear import general as gear_general

from e2e.emulator.dali_fakes import YES, DaliBus, Helvar311Gear, make_gear
from e2e.emulator.installation_profile import GearModel, GearSpec, profile_from_configs
from e2e.emulator.test_dali_fakes import answer, select_gear
from e2e.emulator.test_installation_profile import SERIAL_CONFIG

SHORT = 6
RANDOM = 0x15DBB4


class Helvar311GearTest(unittest.TestCase):
    def setUp(self):
        self.bus = DaliBus([Helvar311Gear(GearSpec(SHORT, RANDOM, GearModel.HELVAR_311, frozenset()))])

    def test_answers_presence_and_the_search(self):
        """QUERY CONTROL GEAR gets YES; in the search COMPARE answers at its random address and
        QUERY SHORT ADDRESS gives its short address, WITHDRAW takes it out."""
        self.assertEqual(answer(self.bus, gear_general.QueryControlGearPresent(GearShort(SHORT))), YES)
        answer(self.bus, gear_general.Initialise(0xFF))
        select_gear(self.bus, RANDOM - 1)
        self.assertIsNone(answer(self.bus, gear_general.Compare()))
        select_gear(self.bus, RANDOM)
        self.assertEqual(answer(self.bus, gear_general.Compare()), YES)
        self.assertEqual(answer(self.bus, gear_general.QueryShortAddress()), (SHORT << 1) | 1)
        answer(self.bus, gear_general.Withdraw())
        self.assertIsNone(answer(self.bus, gear_general.Compare()))

    def test_silent_on_every_other_addressed_query(self):
        """Device type, version, random address, memory and level queries go unanswered."""
        address = GearShort(SHORT)
        for query in (
            gear_general.QueryDeviceType(address),
            gear_general.QueryVersionNumber(address),
            gear_general.QueryRandomAddressH(address),
            gear_general.QueryRandomAddressM(address),
            gear_general.QueryRandomAddressL(address),
            gear_general.ReadMemoryLocation(address),
            gear_general.QueryActualLevel(address),
            gear_general.QueryStatus(address),
        ):
            with self.subTest(query=type(query).__name__):
                self.assertIsNone(answer(self.bus, query))


class Helvar311ProfileTest(unittest.TestCase):
    def test_helvar_311_model_is_emulated_as_one(self):
        """Gear with `model: Helvar 311` in devices-state.json, whatever its name: the Helvar model
        without a GTIN."""
        dali_config = {
            "gateways": [
                {
                    "device_id": "wb-dali_5",
                    "buses": [{"devices": [{"short": SHORT, "random": RANDOM, "name": "Any name"}]}],
                }
            ]
        }
        state = {"1": {"gear": {str(SHORT): {"model": "Helvar 311"}}}}
        spec = profile_from_configs(SERIAL_CONFIG, dali_config, state).buses[0].gear[0]
        self.assertEqual((spec.model, spec.gtin), (GearModel.HELVAR_311, None))
        self.assertIsInstance(make_gear(spec), Helvar311Gear)
