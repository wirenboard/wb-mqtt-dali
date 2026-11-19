import unittest

from dali.gear import general as gear_general

from e2e.emulator.dali_fakes import MASK, YES, DaliBus, EmulatedGear, Helvar311Gear
from e2e.emulator.installation_profile import GearModel, GearSpec
from e2e.emulator.test_dali_fakes import answer, respond, select_gear
from wb.mqtt_dali.commissioning import Commissioning
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress

LAMP_RANDOM = 0x0A0B0C
SECOND_LAMP_RANDOM = 0x0A0B0D
HELVAR_RANDOM = 0x15DBB4


class _FakeBusDriver:
    """What `Commissioning` needs of the driver, answered by the emulated bus."""

    def __init__(self, bus: DaliBus) -> None:
        self.bus = bus
        self.sent: list = []

    async def send(self, cmd, source=None, priority=None, logger=None):
        del source, priority, logger
        self.sent.append(cmd)
        return respond(self.bus, cmd)

    async def send_commands(self, cmds, source=None, priority=None, logger=None):
        return [await self.send(cmd, source, priority, logger) for cmd in cmds]


def _plain(short: int, random_address: int) -> EmulatedGear:
    return EmulatedGear(GearSpec(short, random_address, GearModel.PLAIN, frozenset()))


class GearSearchAnswersTest(unittest.TestCase):
    def setUp(self):
        self.gear = _plain(3, LAMP_RANDOM)
        self.bus = DaliBus([self.gear])

    def test_query_short_address_answers_the_selected_gear_only(self):
        """QUERY SHORT ADDRESS: silent outside initialisation and at another search address; the
        selected gear answers its short address, after WITHDRAW too, and MASK without one."""
        select_gear(self.bus, LAMP_RANDOM)
        self.assertIsNone(answer(self.bus, gear_general.QueryShortAddress()))
        answer(self.bus, gear_general.Initialise(broadcast=True))
        select_gear(self.bus, LAMP_RANDOM + 1)
        self.assertIsNone(answer(self.bus, gear_general.QueryShortAddress()))
        select_gear(self.bus, LAMP_RANDOM)
        self.assertEqual(answer(self.bus, gear_general.QueryShortAddress()), (3 << 1) | 1)
        answer(self.bus, gear_general.Withdraw())
        self.assertEqual(answer(self.bus, gear_general.QueryShortAddress()), (3 << 1) | 1)
        self.gear.shortaddr = None
        self.assertEqual(answer(self.bus, gear_general.QueryShortAddress()), MASK)

    def test_randomise_needs_initialisation(self):
        """RANDOMISE outside initialisation keeps the random address; after INITIALISE it changes."""
        answer(self.bus, gear_general.Randomise())
        self.assertEqual(self.gear.randomaddr.as_integer, LAMP_RANDOM)
        answer(self.bus, gear_general.Initialise(broadcast=True))
        self.gear.random_preload = [0x123456]
        answer(self.bus, gear_general.Randomise())
        self.assertEqual(self.gear.randomaddr.as_integer, 0x123456)


class GearRescanTest(unittest.IsolatedAsyncioTestCase):
    async def test_rescan_keeps_configured_gear_and_helvar(self):
        """wb-mqtt-dali's search over two lamps and a Helvar 311, all in the config: every device
        stays unchanged and no short address is programmed."""
        gear = [
            _plain(0, LAMP_RANDOM),
            _plain(1, SECOND_LAMP_RANDOM),
            Helvar311Gear(GearSpec(6, HELVAR_RANDOM, GearModel.HELVAR_311, frozenset())),
        ]
        driver = _FakeBusDriver(DaliBus(gear))
        config = [
            DaliDeviceAddress(0, LAMP_RANDOM),
            DaliDeviceAddress(1, SECOND_LAMP_RANDOM),
            DaliDeviceAddress(6, HELVAR_RANDOM),
        ]

        result = await Commissioning(driver, config).smart_extend()

        self.assertEqual(sorted(result.unchanged, key=lambda a: a.short), config)
        self.assertEqual((result.new, result.missing, result.changed), ([], [], []))
        self.assertFalse(any(isinstance(cmd, gear_general.ProgramShortAddress) for cmd in driver.sent))

    async def test_rescan_addresses_gear_without_short_address(self):
        """A lamp without a short address next to an addressed one: the search finds it by its
        random address and gives it the first free short address, which it then answers on."""
        unaddressed = _plain(0, SECOND_LAMP_RANDOM)
        unaddressed.shortaddr = None
        driver = _FakeBusDriver(DaliBus([_plain(0, LAMP_RANDOM), unaddressed]))

        result = await Commissioning(driver, [DaliDeviceAddress(0, LAMP_RANDOM)]).smart_extend()

        self.assertEqual(result.unchanged, [DaliDeviceAddress(0, LAMP_RANDOM)])
        self.assertEqual(result.new, [DaliDeviceAddress(1, SECOND_LAMP_RANDOM)])
        self.assertEqual(unaddressed.shortaddr, 1)
        self.assertEqual(driver.bus.transact(gear_general.QueryControlGearPresent(1).frame).backward, YES)
