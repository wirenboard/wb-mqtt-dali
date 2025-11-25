import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from dali.gear.general import (
    Compare,
    Initialise,
    ProgramShortAddress,
    QueryControlGearPresent,
    QueryRandomAddressH,
    QueryRandomAddressL,
    QueryRandomAddressM,
    QueryShortAddress,
    Randomise,
    SearchaddrH,
    SearchaddrL,
    SearchaddrM,
    SetSearchAddrH,
    SetSearchAddrL,
    SetSearchAddrM,
    Terminate,
    VerifyShortAddress,
    Withdraw,
)

from wb.mqtt_dali.commissioning import (
    BinarySearchAddressFinder,
    Commissioning,
    SearchAddress,
)


def search_sequence(cmd_cls, values):
    seq = []
    for v in values:
        seq.append(cmd_cls(v))
        seq.append(Compare())
    return seq


class MockResponse:
    def __init__(self, value=None, raw_value=None, error=False):
        self.value = value
        if raw_value is not None:
            self.raw_value = raw_value
        elif value is None:
            self.raw_value = None
        else:
            self.raw_value = Mock()
            self.raw_value.error = error


class TestBinarySearchAddressFinder(unittest.IsolatedAsyncioTestCase):
    async def test_find_device_at_exact_midpoint(self):
        """Test finding a device when it's exactly at the midpoint of the search range."""
        device_address = 0x800000
        compare_calls = []
        set_search_calls = []

        async def mock_compare(addr):
            compare_calls.append(addr)
            return addr >= device_address

        async def mock_set_search(addr):
            set_search_calls.append(addr)

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x000000, 0xFFFFFF)

        self.assertEqual(result, device_address)
        self.assertIn(0xFFFFFF, compare_calls)
        self.assertIn(device_address, compare_calls)

    async def test_find_device_at_lower_bound(self):
        """Test finding a device at the lower bound of the search range."""
        device_address = 0x000000
        compare_calls = []

        async def mock_compare(addr):
            compare_calls.append(addr)
            return addr >= device_address

        async def mock_set_search(addr):
            pass

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x000000, 0xFFFFFF)

        self.assertEqual(result, device_address)

    async def test_find_device_at_upper_bound(self):
        """Test finding a device at the upper bound of the search range."""
        device_address = 0xFFFFFF
        set_search_calls = []

        async def mock_compare(addr):
            return addr >= device_address

        async def mock_set_search(addr):
            set_search_calls.append(addr)

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x000000, 0xFFFFFF)

        self.assertEqual(result, device_address)
        self.assertIn(device_address, set_search_calls)

    async def test_no_device_found(self):
        """Test when no device responds in the search range."""

        async def mock_compare(addr):
            return False

        async def mock_set_search(addr):
            pass

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x000000, 0xFFFFFF)

        self.assertIsNone(result)

    async def test_find_device_in_small_range(self):
        """Test finding a device in a small address range (2 addresses)."""
        device_address = 0x000010

        async def mock_compare(addr):
            return addr >= device_address

        async def mock_set_search(addr):
            pass

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x00000F, 0x000011)

        self.assertEqual(result, device_address)

    async def test_find_device_with_specific_address(self):
        """Test finding a device at a specific address and verify the binary search path."""
        device_address = 0x123456
        compare_calls = []

        async def mock_compare(addr):
            compare_calls.append(addr)
            return addr >= device_address

        async def mock_set_search(addr):
            pass

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x000000, 0xFFFFFF)

        self.assertEqual(result, device_address)
        self.assertGreater(len(compare_calls), 2)
        self.assertEqual(compare_calls[0], 0xFFFFFF)

    async def test_binary_search_efficiency(self):
        """Test that binary search is efficient (logarithmic comparisons)."""
        device_address = 0xABCDEF
        compare_calls = []

        async def mock_compare(addr):
            compare_calls.append(addr)
            return addr >= device_address

        async def mock_set_search(addr):
            pass

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x000000, 0xFFFFFF)

        self.assertEqual(result, device_address)
        # For 24-bit address space, binary search should take ~24 comparisons max
        self.assertLess(len(compare_calls), 30)

    async def test_find_device_near_zero(self):
        """Test finding a device very close to address 0."""
        device_address = 0x000005

        async def mock_compare(addr):
            return addr >= device_address

        async def mock_set_search(addr):
            pass

        finder = BinarySearchAddressFinder(mock_compare, mock_set_search)
        result = await finder.find_next_device(0x000000, 0xFFFFFF)

        self.assertEqual(result, device_address)


class FakeDALIBus:
    def __init__(self, devices=None):
        self.devices = devices or {}  # short_addr: random_addr
        self.withdrawn = set()
        self.search_addr = [None, None, None]
        self.initialized_shorts = set()
        self.broadcast_initialized = False
        self.next_random = 0x200000

    def reset(self):
        self.withdrawn.clear()
        self.initialized_shorts.clear()
        self.broadcast_initialized = False
        self.search_addr = [None, None, None]

    async def send(self, cmd):
        # Handle QueryControlGearPresent
        if isinstance(cmd, QueryControlGearPresent):
            short = cmd.destination.address
            if short in self.devices:
                return MockResponse(value=True)
            return MockResponse(value=False)

        # Handle QueryRandomAddress commands
        if isinstance(cmd, (QueryRandomAddressH, QueryRandomAddressM, QueryRandomAddressL)):
            short = cmd.destination.address
            if short not in self.devices:
                return MockResponse(value=None)

            rand_addr = self.devices[short]
            if isinstance(cmd, QueryRandomAddressH):
                value_int = (rand_addr >> 16) & 0xFF
            elif isinstance(cmd, QueryRandomAddressM):
                value_int = (rand_addr >> 8) & 0xFF
            else:
                value_int = rand_addr & 0xFF

            mock_value = Mock()
            mock_value.as_integer = value_int
            return MockResponse(value=mock_value)

        # Handle SetSearchAddr commands
        if isinstance(cmd, SetSearchAddrH):
            self.search_addr[0] = cmd.param
            return MockResponse(value=None)
        if isinstance(cmd, SetSearchAddrM):
            self.search_addr[1] = cmd.param
            return MockResponse(value=None)
        if isinstance(cmd, SetSearchAddrL):
            self.search_addr[2] = cmd.param
            return MockResponse(value=None)

        # Handle Compare
        if isinstance(cmd, Compare):
            if None in self.search_addr:
                return MockResponse(value=False)

            search = (self.search_addr[0] << 16) | (self.search_addr[1] << 8) | self.search_addr[2]

            for short, rand in self.devices.items():
                if rand not in self.withdrawn and rand <= search:
                    return MockResponse(value=True)
            return MockResponse(value=False)

        # Handle QueryShortAddress
        if isinstance(cmd, QueryShortAddress):
            if None in self.search_addr:
                return MockResponse(value=None, raw_value=None)

            search = (self.search_addr[0] << 16) | (self.search_addr[1] << 8) | self.search_addr[2]

            matching_devices = [
                short for short, rand in self.devices.items() if rand == search and rand not in self.withdrawn
            ]

            if len(matching_devices) == 0:
                return MockResponse(value=None, raw_value=None)
            if len(matching_devices) == 1:
                short = matching_devices[0]
                raw_val = Mock()
                raw_val.error = False
                return MockResponse(value=(short << 1) | 1, raw_value=raw_val)

            raw_val = Mock()
            raw_val.error = True
            return MockResponse(value=None, raw_value=raw_val)

        # Handle Withdraw
        if isinstance(cmd, Withdraw):
            if None not in self.search_addr:
                search = (self.search_addr[0] << 16) | (self.search_addr[1] << 8) | self.search_addr[2]
                self.withdrawn.add(search)
            return MockResponse(value=None)

        # Handle Terminate
        if isinstance(cmd, Terminate):
            self.broadcast_initialized = False
            self.initialized_shorts.clear()
            return MockResponse(value=None)

        # Handle Initialise
        if isinstance(cmd, Initialise):
            if cmd.broadcast:
                self.broadcast_initialized = True
            elif cmd.address is not None:
                self.initialized_shorts.add(cmd.address)
            return MockResponse(value=None)

        # Handle Randomise
        if isinstance(cmd, Randomise):
            if self.broadcast_initialized:
                for short in list(self.devices.keys()):
                    self.devices[short] = self.next_random
                    self.next_random += 1
            else:
                for short in self.initialized_shorts:
                    if short in self.devices:
                        self.devices[short] = self.next_random
                        self.next_random += 1
            return MockResponse(value=None)

        # Handle ProgramShortAddress
        if isinstance(cmd, ProgramShortAddress):
            if None not in self.search_addr:
                search = (self.search_addr[0] << 16) | (self.search_addr[1] << 8) | self.search_addr[2]
                for old_short, rand in list(self.devices.items()):
                    if rand == search:
                        del self.devices[old_short]
                        if cmd.address != "MASK":
                            self.devices[cmd.address] = rand
                        break
            return MockResponse(value=None)

        # Handle VerifyShortAddress
        if isinstance(cmd, VerifyShortAddress):
            short_addr = cmd.destination.address
            if short_addr in self.devices:
                return MockResponse(value=True)
            return MockResponse(value=None)

        return MockResponse(value=None)


class TestCommissioning(unittest.TestCase):
    def setUp(self):
        self.mock_driver = MagicMock()

        self.temp_file = tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".json")
        self.temp_file_path = self.temp_file.name
        self.temp_file.close()

    def tearDown(self):
        if os.path.exists(self.temp_file_path):
            os.unlink(self.temp_file_path)

    def assert_commands_match(self, sent_commands, expected_commands):
        self.assertEqual(len(sent_commands), len(expected_commands))
        for i, (sent, expected) in enumerate(zip(sent_commands, expected_commands)):
            self.assertEqual(
                type(sent),
                type(expected),
                f"Command {i}: expected {type(expected).__name__}, got {type(sent).__name__}",
            )
            if isinstance(
                sent,
                (
                    QueryControlGearPresent,
                    QueryRandomAddressH,
                    QueryRandomAddressM,
                    QueryRandomAddressL,
                    QueryRandomAddressH,
                    QueryRandomAddressM,
                    QueryRandomAddressL,
                ),
            ):
                self.assertEqual(
                    sent.destination.address,
                    expected.destination.address,
                    f"Command {i}: destination address mismatch",
                )
            if isinstance(
                sent,
                (
                    SearchaddrH,
                    SearchaddrM,
                    SearchaddrL,
                ),
            ):
                self.assertEqual(
                    sent.param,
                    expected.param,
                    f"Command {i}: param mismatch",
                )
            if isinstance(sent, Initialise):
                self.assertEqual(
                    sent.broadcast,
                    expected.broadcast,
                    f"Command {i}: Initialise broadcast mismatch",
                )
                self.assertEqual(
                    sent.address,
                    expected.address,
                    f"Command {i}: Initialise address mismatch",
                )
            if isinstance(sent, (ProgramShortAddress, VerifyShortAddress)):
                self.assertEqual(
                    sent.address,
                    expected.address,
                    f"Command {i}: address mismatch",
                )

    def test_load_results(self):
        """Test loading results from a JSON file."""
        test_data = {
            "version": 1,
            "generated_at": "2023-12-01T10:30:00Z",
            "entries": [
                {"short": 1, "random": 0x123456, "random_hex": "0x123456"},
                {"short": 2, "random": 0x789ABC, "random_hex": "0x789ABC"},
                {"short": 5, "random": 0xDEF123, "random_hex": "0xDEF123"},
            ],
        }

        with open(self.temp_file_path, "w", encoding="utf-8") as f:
            json.dump(test_data, f)

        with patch("wb.mqtt_dali.commissioning.log") as mock_log:
            commissioning = Commissioning(self.mock_driver, self.temp_file_path, load=False)
            commissioning._load_results()  # pylint: disable=W0212

        expected_devices = {1: 0x123456, 2: 0x789ABC, 5: 0xDEF123}
        self.assertEqual(commissioning.old_devices, expected_devices)

        mock_log.info.assert_called_with(
            "Loaded %d device entries (old devices) from %s", 3, self.temp_file_path
        )

    @patch("wb.mqtt_dali.commissioning.datetime")
    def test_save_results(self, mock_datetime):
        """Test saving results to a JSON file."""
        mock_datetime.utcnow.return_value.isoformat.return_value = "2023-12-01T10:30:00"

        commissioning = Commissioning(self.mock_driver, self.temp_file_path, load=False)
        commissioning.known_devices = {1: 0x123456, 3: 0xDEF123}
        commissioning.found_devices = {2: 0x789ABC}

        with patch("wb.mqtt_dali.commissioning.log") as mock_log:
            commissioning._save_results()  # pylint: disable=W0212

        self.assertTrue(os.path.exists(self.temp_file_path))

        with open(self.temp_file_path, "r", encoding="utf-8") as f:
            saved_data = json.load(f)

        expected_data = {
            "version": 1,
            "generated_at": "2023-12-01T10:30:00Z",
            "entries": [
                {"short": 1, "random": 0x123456, "random_hex": "0x123456"},
                {"short": 2, "random": 0x789ABC, "random_hex": "0x789abc"},
                {"short": 3, "random": 0xDEF123, "random_hex": "0xdef123"},
            ],
        }

        self.assertEqual(saved_data, expected_data)

        mock_log.info.assert_called_with(
            "Saved %d device entries (known + new) to %s", 3, self.temp_file_path
        )

    def test_set_search_addr(self):
        """Test setting search address."""
        self.commissioning = Commissioning(self.mock_driver, None, load=False)
        self.assertEqual(self.commissioning.last_search_addr, SearchAddress(None, None, None))

        commands = list(self.commissioning._set_search_addr(0x123456))  # pylint: disable=W0212

        self.assertEqual(len(commands), 3)

        self.assertIsInstance(commands[0], SetSearchAddrH)
        self.assertIsInstance(commands[1], SetSearchAddrM)
        self.assertIsInstance(commands[2], SetSearchAddrL)

        expected_addr = SearchAddress(high=0x12, medium=0x34, low=0x56)
        self.assertEqual(self.commissioning.last_search_addr, expected_addr)

    def test_smart_extend_simple_case(self):
        """Test smart_extend with a simple case of two devices.

        Initial State:
        - No prior commissioning state exists (load=False)
        - Two devices are physically present on the bus
        - Each device has a unique short and random address

        Short Addr | Random Addr
        -------------------------
        0          | 0x123456
        1          | 0x789ABC
        2-63       | Unused

        Expected Behavior:
        - Exactly 2 devices are discovered
        - No changes of short/random addresses
        """

        async def run_test():
            fake_bus = FakeDALIBus(devices={0: 0x123456, 1: 0x789ABC})

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)

            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 2)
            self.assertEqual(commissioning.found_devices[0], 0x123456)
            self.assertEqual(commissioning.found_devices[1], 0x789ABC)

        asyncio.run(run_test())

    def test_smart_extend_binary_search_new_devices(self):
        """Test smart_extend with binary search for new devices.

        Initial State:
        - No prior commissioning state exists (load=False)
        - One device is physically present on the bus
        - Device has no short address assigned yet
        - Device has a factory-assigned random address: 0x555555

        Short Addr | Random Addr
        -------------------------
        None       | 0x123456
        1-63       | Unused

        Expected Behavior:
        - Exactly 1 device is discovered via binary search
        - Device is assigned short address 0 (first available)
        - No changes of random address
        """

        async def run_test():
            fake_bus = FakeDALIBus(devices={})

            test_random = 0x123456

            sent_commands = []
            original_send = fake_bus.send

            async def mock_send_with_unaddressed(cmd):
                sent_commands.append(cmd)
                if isinstance(cmd, Compare):
                    if None not in fake_bus.search_addr:
                        search = (
                            (fake_bus.search_addr[0] << 16)
                            | (fake_bus.search_addr[1] << 8)
                            | fake_bus.search_addr[2]
                        )
                        if test_random not in fake_bus.withdrawn and test_random <= search:
                            return MockResponse(value=True)
                    return MockResponse(value=False)
                if isinstance(cmd, QueryShortAddress):
                    if None not in fake_bus.search_addr:
                        search = (
                            (fake_bus.search_addr[0] << 16)
                            | (fake_bus.search_addr[1] << 8)
                            | fake_bus.search_addr[2]
                        )
                        if search == test_random and test_random not in fake_bus.withdrawn:
                            return MockResponse(value="MASK")
                    return MockResponse(value=None)
                if isinstance(cmd, ProgramShortAddress):
                    if cmd.address != "MASK":
                        fake_bus.devices[cmd.address] = test_random
                    return MockResponse(value=None)
                if isinstance(cmd, VerifyShortAddress):
                    return MockResponse(value=True)

                return await original_send(cmd)

            fake_bus.send = mock_send_with_unaddressed

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            # Device should be found and assigned address 0
            self.assertIn(0, commissioning.found_devices)
            self.assertEqual(commissioning.found_devices[0], test_random)

            expected_commands = [
                *[QueryControlGearPresent(destination=i) for i in range(64)],
                Terminate(),
                Initialise(broadcast=True),
                SearchaddrH(255),
                SearchaddrM(255),
                SearchaddrL(255),
                Compare(),
                *search_sequence(SearchaddrH, [127, 63, 31, 15, 23, 19, 17, 18]),
                *search_sequence(SearchaddrM, [127, 63, 31, 47, 55, 51, 53, 52]),
                *search_sequence(SearchaddrL, [127, 63, 95, 79, 87, 83, 85, 86, 85]),
                SearchaddrL(86),
                QueryShortAddress(),
                Withdraw(),
                ProgramShortAddress(0),
                VerifyShortAddress(0),
                SearchaddrH(255),
                SearchaddrM(255),
                SearchaddrL(255),
                Compare(),
                Terminate(),
            ]

            self.assert_commands_match(sent_commands, expected_commands)

        asyncio.run(run_test())

    def test_smart_extend_duplicate_random_addresses(self):
        """Test smart_extend handling duplicate random addresses.

        Initial State:
        - No prior commissioning state exists (load=False)
        - Two devices are physically present on the bus
        - Both devices have short addresses already assigned (0 and 1)
        - Both devices have the same random address: 0x111111

        Short Addr | Random Addr
        -------------------------
        0          | 0x111111
        1          | 0x111111
        2-63       | Unused

        Expected Behavior:
        - Correct sequence of INITIALISE and RANDOMISE commands
        - New random addresses are assigned to devices
        """

        async def run_test():
            duplicate_random = 0x111111
            fake_bus = FakeDALIBus(devices={0: duplicate_random, 1: duplicate_random})

            sent_commands = []
            original_send = fake_bus.send

            async def track_commands(cmd):
                sent_commands.append(cmd)
                return await original_send(cmd)

            fake_bus.send = track_commands

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)

            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 2)
            self.assertEqual(commissioning.found_devices[0], 0x200000)
            self.assertEqual(commissioning.found_devices[1], 0x200001)

            expected_commands = [
                *[QueryControlGearPresent(destination=i) for i in range(64)],
                QueryRandomAddressH(destination=0),
                QueryRandomAddressM(destination=0),
                QueryRandomAddressL(destination=0),
                QueryRandomAddressH(destination=1),
                QueryRandomAddressM(destination=1),
                QueryRandomAddressL(destination=1),
                Terminate(),
                Initialise(broadcast=False, address=0),
                Randomise(),
                Terminate(),
                Initialise(broadcast=False, address=1),
                Randomise(),
                QueryRandomAddressH(destination=0),
                QueryRandomAddressM(destination=0),
                QueryRandomAddressL(destination=0),
                QueryRandomAddressH(destination=1),
                QueryRandomAddressM(destination=1),
                QueryRandomAddressL(destination=1),
                Terminate(),
                Initialise(broadcast=True),
                SearchaddrH(32),
                SearchaddrM(0),
                SearchaddrL(0),
                QueryShortAddress(),
                Withdraw(),
                SearchaddrL(1),
                QueryShortAddress(),
                Withdraw(),
                SearchaddrH(255),
                SearchaddrM(255),
                SearchaddrL(255),
                Compare(),
                Terminate(),
            ]

            self.assert_commands_match(sent_commands, expected_commands)

        asyncio.run(run_test())

    def test_smart_extend_unset_random_address(self):
        """Test smart_extend handling unset random addresses.

        Initial State:
        - No prior commissioning state exists (load=False)
        - One device is physically present on the bus at short address 5
        - Device has an unset/invalid random address: 0xFFFFFF

        Short Addr | Random Addr
        -------------------------
        5          | 0xFFFFFF
        0-4,6-63   | Unused

        Expected Behavior:
        - Correct sequence of INITIALISE and RANDOMISE commands
        """

        async def run_test():
            fake_bus = FakeDALIBus(devices={5: 0xFFFFFF})

            sent_commands = []
            original_send = fake_bus.send

            async def track_commands(cmd):
                sent_commands.append(cmd)
                return await original_send(cmd)

            fake_bus.send = track_commands

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 1)
            self.assertEqual(commissioning.found_devices[5], 0x200000)

            expected_commands = [
                *[QueryControlGearPresent(destination=i) for i in range(64)],
                QueryRandomAddressH(destination=5),
                QueryRandomAddressM(destination=5),
                QueryRandomAddressL(destination=5),
                Terminate(),
                Initialise(broadcast=False, address=5),
                Randomise(),
                QueryRandomAddressH(destination=5),
                QueryRandomAddressM(destination=5),
                QueryRandomAddressL(destination=5),
                Terminate(),
                Initialise(broadcast=True),
                SearchaddrH(32),
                SearchaddrM(0),
                SearchaddrL(0),
                QueryShortAddress(),
                Withdraw(),
                SearchaddrH(255),
                SearchaddrM(255),
                SearchaddrL(255),
                Compare(),
                Terminate(),
            ]

            self.assert_commands_match(sent_commands, expected_commands)

        asyncio.run(run_test())

    def test_smart_extend_preserve_old_addresses(self):
        """Test smart_extend preserving old addresses from state file.

        Initial State:
        - Prior commissioning state exists in state file (load=True)
        - State file contains 2 devices at addresses 3 and 7
        - Both devices are still present on the bus with same random addresses
        - No devices have been added, removed, or changed addresses

        Short Addr | Random Addr
        -------------------------
        3            | 0xAAAAAA
        7            | 0xBBBBBB
        0-2,4-6,8-63 | Unused

        Expected Behavior:
        - found_devices contains both devices with their original short addresses
        - Device at address 3 has random address 0xAAAAAA
        - Device at address 7 has random address 0xBBBBBB
        - No devices moved to different short addresses
        """

        async def run_test():
            state_data = {
                "version": 1,
                "generated_at": "2023-12-01T10:00:00Z",
                "entries": [
                    {"short": 3, "random": 0xAAAAAA, "random_hex": "0xaaaaaa"},
                    {"short": 7, "random": 0xBBBBBB, "random_hex": "0xbbbbbb"},
                ],
            }

            with open(self.temp_file_path, "w", encoding="utf-8") as f:
                json.dump(state_data, f)

            fake_bus = FakeDALIBus(devices={3: 0xAAAAAA, 7: 0xBBBBBB})

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=True)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 2)
            self.assertEqual(commissioning.found_devices.get(3), 0xAAAAAA)
            self.assertEqual(commissioning.found_devices.get(7), 0xBBBBBB)

        asyncio.run(run_test())

    def test_smart_extend_changed_short_address(self):
        """Test smart_extend handling changed short addresses.

        Initial State:
        - Prior commissioning state exists in state file (load=True)
        - State file shows device with random address 0xCCCCCC at short address 5
        - Same device (same random address) is now found at short address 10
        - The device's random address hasn't changed, only its short address

        Short Addr | Random Addr
        -------------------------
        10         | 0xCCCCCC
        0-9,11-63  | Unused

        Expected Behavior:
        - found_devices[10] contains the random address 0xCCCCCC (new location)
        - old_devices[5] contains the random address 0xCCCCCC (historical record)
        """

        async def run_test():
            state_data = {
                "version": 1,
                "generated_at": "2023-12-01T10:00:00Z",
                "entries": [
                    {"short": 5, "random": 0xCCCCCC, "random_hex": "0xcccccc"},
                ],
            }

            with open(self.temp_file_path, "w", encoding="utf-8") as f:
                json.dump(state_data, f)

            fake_bus = FakeDALIBus(devices={10: 0xCCCCCC})

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=True)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 1)
            self.assertEqual(commissioning.found_devices.get(10), 0xCCCCCC)
            self.assertEqual(len(commissioning.old_devices), 1)
            self.assertEqual(commissioning.old_devices.get(5), 0xCCCCCC)

        asyncio.run(run_test())

    def test_smart_extend_new_device_added(self):
        """Test smart_extend adding new devices alongside old ones.

        Initial State:
        - Prior commissioning state exists in state file (load=True)
        - State file contains 1 device at address 1 with random address 0x111111
        - A new device has been physically added to the bus at address 2
        - The new device has random address 0x333333
        - Existing device remains unchanged

        Short Addr | Random Addr
        -------------------------
        1          | 0x111111
        2          | 0x333333
        3-63       | Unused

        Expected Behavior:
        - found_devices contains exactly 2 devices
        - found_devices[1] = 0x111111 (existing device preserved)
        - found_devices[2] = 0x333333 (new device discovered)
        """

        async def run_test():
            state_data = {
                "version": 1,
                "generated_at": "2023-12-01T10:00:00Z",
                "entries": [
                    {"short": 1, "random": 0x111111, "random_hex": "0x111111"},
                ],
            }

            with open(self.temp_file_path, "w", encoding="utf-8") as f:
                json.dump(state_data, f)

            fake_bus = FakeDALIBus(devices={1: 0x111111, 2: 0x333333})

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=True)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 2)
            self.assertEqual(commissioning.found_devices.get(1), 0x111111)
            self.assertEqual(commissioning.found_devices.get(2), 0x333333)

        asyncio.run(run_test())

    def test_smart_extend_available_addresses_tracking(self):
        """Test smart_extend tracking available addresses.
        Short Addr | Random Addr
        -------------------------
        0          | 0x100000
        1          | 0x200000
        2          | 0x300000
        3          | 0x400000
        4          | 0x500000
        5          | 0x600000
        6-63       | Unused

        Found Devices: 6
        """

        async def run_test():
            fake_bus = FakeDALIBus(
                devices={
                    0: 0x100000,
                    1: 0x200000,
                    2: 0x300000,
                    3: 0x400000,
                    4: 0x500000,
                    5: 0x600000,
                }
            )

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 6)

            for i in range(6):
                self.assertNotIn(i, commissioning.available_addresses)

            self.assertIn(6, commissioning.available_addresses)

        asyncio.run(run_test())

    def test_smart_extend_state_file_persistence(self):
        """Test smart_extend saving state file correctly.
        Bus:
        Short Addr | Random Addr
        -------------------------
        0          | 0xABCDEF
        1          | 0x123456
        2-63       | Unused
        """

        async def run_test():
            fake_bus = FakeDALIBus(devices={0: 0xABCDEF, 1: 0x123456})

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            self.assertTrue(os.path.exists(self.temp_file_path))

            with open(self.temp_file_path, "r", encoding="utf-8") as f:
                saved_data = json.load(f)

            self.assertEqual(saved_data["version"], 1)
            self.assertEqual(len(saved_data["entries"]), 2)

            entries_by_short = {e["short"]: e for e in saved_data["entries"]}
            self.assertEqual(entries_by_short[0]["random"], 0xABCDEF)
            self.assertEqual(entries_by_short[1]["random"], 0x123456)

        asyncio.run(run_test())

    def test_smart_extend_binary_search_boundaries(self):
        """Test smart_extend with devices at boundary random addresses.

        Initial State:
        - No prior commissioning state exists (load=False)
        - Three devices are physically present on the bus
        - Devices have no short addresses assigned yet
        - Each device has a random address at a boundary position

        Short Addr | Random Addr
        -------------------------
        None       | 0x000001
        None       | 0xFFFFFE
        None       | 0x800000
        0-63       | Unused

        Expected Behavior:
        - Exactly 3 devices are discovered
        - All boundary addresses (min, max, mid) are found correctly
        """

        async def run_test():
            fake_bus = FakeDALIBus(devices={})

            test_devices = [
                0x000001,
                0xFFFFFE,
                0x800000,
            ]

            original_send = fake_bus.send

            async def mock_boundary_send(cmd):
                if isinstance(cmd, Compare):
                    if None not in fake_bus.search_addr:
                        search = (
                            (fake_bus.search_addr[0] << 16)
                            | (fake_bus.search_addr[1] << 8)
                            | fake_bus.search_addr[2]
                        )
                        for rand in test_devices:
                            if rand not in fake_bus.withdrawn and rand <= search:
                                return MockResponse(value=True)
                    return MockResponse(value=False)
                if isinstance(cmd, QueryShortAddress):
                    if None not in fake_bus.search_addr:
                        search = (
                            (fake_bus.search_addr[0] << 16)
                            | (fake_bus.search_addr[1] << 8)
                            | fake_bus.search_addr[2]
                        )
                        for rand in test_devices:
                            if rand == search and rand not in fake_bus.withdrawn:
                                raw_val = Mock()
                                raw_val.error = False
                                return MockResponse(value="MASK", raw_value=raw_val)
                    return MockResponse(value=None, raw_value=None)
                if isinstance(cmd, ProgramShortAddress):
                    if cmd.address != "MASK":
                        if None not in fake_bus.search_addr:
                            search = (
                                (fake_bus.search_addr[0] << 16)
                                | (fake_bus.search_addr[1] << 8)
                                | fake_bus.search_addr[2]
                            )
                            fake_bus.devices[cmd.address] = search
                    return MockResponse(value=None)
                if isinstance(cmd, VerifyShortAddress):
                    return MockResponse(value=True)

                return await original_send(cmd)

            fake_bus.send = mock_boundary_send

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 3)
            self.assertEqual(commissioning.found_devices.get(0), 0x000001)
            self.assertEqual(commissioning.found_devices.get(1), 0x800000)
            self.assertEqual(commissioning.found_devices.get(2), 0xFFFFFE)

        asyncio.run(run_test())

    def test_smart_extend_empty_bus(self):
        """Test smart_extend on an empty bus.
        Short Addr | Random Addr
        -------------------------
        0-63       | Unused

        Found Devices: 0
        Available Addresses: 64
        """

        async def run_test():
            fake_bus = FakeDALIBus(devices={})

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 0)
            self.assertEqual(len(commissioning.available_addresses), 64)

        asyncio.run(run_test())

    def test_smart_extend_full_bus(self):
        """Test smart_extend on a full bus.
        Short Addr | Random Addr
        -------------------------
        0-63       | 0x100000 - 0x10003F

        Found Devices: 64
        Available Addresses: 0
        """

        async def run_test():
            devices = {i: 0x100000 + i for i in range(64)}
            fake_bus = FakeDALIBus(devices=devices)

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            self.assertEqual(len(commissioning.found_devices), 64)
            self.assertEqual(len(commissioning.available_addresses), 0)

        asyncio.run(run_test())

    def test_smart_extend_search_addr_optimization(self):
        """Test smart_extend optimizing search address commands.

        Initial State:
        - No prior commissioning state exists (load=False)
        - Three devices are physically present on the bus
        - Devices already have short addresses assigned (0, 1, 2)
        - Devices have similar random addresses (first two share high/medium bytes)

        Short Addr | Random Addr
        -------------------------
        0          | 0x123456
        1          | 0x123457
        2          | 0x234567
        3-63       | Unused

        Query Known Random Addresses:
         - Device 0: 3 calls (H,M,L)
         - Device 1: 1 call (L) - H,M same as Device 0
         - Device 2: 3 calls (H,M,L)

        Binary Search:
         - compare 0xFFFFFF: 3 calls (H,M,L)

        Total SetSearchAddr calls: 10
        """

        async def run_test():
            fake_bus = FakeDALIBus(devices={0: 0x123456, 1: 0x123457, 2: 0x234567})

            set_search_calls = []
            original_send = fake_bus.send

            async def track_set_search(cmd):
                if isinstance(cmd, (SetSearchAddrH, SetSearchAddrM, SetSearchAddrL)):
                    set_search_calls.append(type(cmd).__name__)
                return await original_send(cmd)

            fake_bus.send = track_set_search

            commissioning = Commissioning(fake_bus, self.temp_file_path, load=False)
            await commissioning.smart_extend()

            expected_calls = [
                # Device 0 (0x123456): first time, must send all 3 bytes
                "SearchaddrH",
                "SearchaddrM",
                "SearchaddrL",
                # Device 1 (0x123457): H,M same as cached, only L changes
                "SearchaddrL",
                # Device 2 (0x234567): all different from cached
                "SearchaddrH",
                "SearchaddrM",
                "SearchaddrL",
                # Binary search (0xFFFFFF): all different from cached
                "SearchaddrH",
                "SearchaddrM",
                "SearchaddrL",
            ]

            self.assertEqual(set_search_calls, expected_calls)

        asyncio.run(run_test())
