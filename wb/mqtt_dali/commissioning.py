#!/usr/bin/env python3
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from dali.address import DeviceBroadcast, DeviceShort, GearShort
from dali.command import Command, NumericResponse, NumericResponseMask
from dali.device import general as control_device
from dali.exceptions import ResponseError
from dali.gear import general as control_gear

from .dali_device import DaliDeviceAddress
from .wbdali import MASK, AsyncDeviceInstanceTypeMapper, WBDALIDriver

log = logging.getLogger("commissioning")


@dataclass
class ChangedDevice:
    new: DaliDeviceAddress
    old_short: int


@dataclass
class CommissioningResult:
    unchanged: list[DaliDeviceAddress] = field(default_factory=list)
    changed: list[ChangedDevice] = field(default_factory=list)
    missing: list[DaliDeviceAddress] = field(default_factory=list)
    new: list[DaliDeviceAddress] = field(default_factory=list)


@dataclass
class SearchAddress:
    """Represents a 24-bit DALI search address split into three 8-bit components."""

    high: Optional[int] = None
    medium: Optional[int] = None
    low: Optional[int] = None

    @classmethod
    def from_int(cls, addr: int) -> "SearchAddress":
        """Create SearchAddress from a 24-bit integer."""
        return cls(
            high=(addr >> 16) & 0xFF,
            medium=(addr >> 8) & 0xFF,
            low=addr & 0xFF,
        )


class BinarySearchAddressFinder:  # pylint: disable=R0903
    def __init__(self, compare_callback: Callable, set_search_addr_callback: Callable):
        self.compare = compare_callback
        self.set_search_addr = set_search_addr_callback

    async def find_next_device(self, low: int, high: int) -> Optional[int]:
        if not await self.compare(high):
            log.info("No device left to address, exiting")
            return None

        while high - low > 1:
            midpoint = (low + high) // 2
            if await self.compare(midpoint):
                # Device responds - search lower half
                high = midpoint
            else:
                # No response - search upper half
                low = midpoint

        # Check which of the two remaining addresses is the device
        if await self.compare(low):
            found_addr = low
        else:
            found_addr = high
            await self.set_search_addr(high)

        return found_addr


class CommandsCompatibilityLayer:
    def __init__(self, dali2: bool = False) -> None:
        if dali2:
            self.Compare = control_device.Compare
            self.QueryShortAddress = control_device.QueryShortAddress
            self.Randomise = control_device.Randomise
            self.Terminate = control_device.Terminate
            self.VerifyShortAddress = control_device.VerifyShortAddress
            self.Withdraw = control_device.Withdraw
            self.SetSearchAddrH = control_device.SearchAddrH
            self.SetSearchAddrM = control_device.SearchAddrM
            self.SetSearchAddrL = control_device.SearchAddrL
        else:
            self.Compare = control_gear.Compare
            self.QueryShortAddress = control_gear.QueryShortAddress
            self.Randomise = control_gear.Randomise
            self.Terminate = control_gear.Terminate
            self.VerifyShortAddress = control_gear.VerifyShortAddress
            self.Withdraw = control_gear.Withdraw
            self.SetSearchAddrH = control_gear.SetSearchAddrH
            self.SetSearchAddrM = control_gear.SetSearchAddrM
            self.SetSearchAddrL = control_gear.SetSearchAddrL

        self._is_dali2 = dali2

    def Initialise(self, short_addr: Optional[int]) -> Command:
        if self._is_dali2:
            if short_addr is None:
                return control_device.Initialise(255)
            return control_device.Initialise(short_addr)
        if short_addr is None or short_addr == MASK:
            return control_gear.Initialise(broadcast=True)
        return control_gear.Initialise(address=short_addr, broadcast=False)

    def ProgramShortAddress(self, short_addr: int) -> Command:
        if self._is_dali2:
            return control_device.ProgramShortAddress(short_addr)
        if short_addr == MASK:
            return control_gear.ProgramShortAddress("MASK")
        return control_gear.ProgramShortAddress(short_addr)

    def QueryShortAddressResponseValue(
        self, resp: Union[NumericResponse, NumericResponseMask]
    ) -> Optional[int]:
        value = resp.value
        if isinstance(value, int):
            if self._is_dali2:
                return value
            return value >> 1
        if value == "MASK":
            return MASK
        return None

    def QueryRandomAddressH(self, addr: int):
        if self._is_dali2:
            return control_device.QueryRandomAddressH(DeviceShort(addr))
        return control_gear.QueryRandomAddressH(GearShort(addr))

    def QueryRandomAddressM(self, addr: int):
        if self._is_dali2:
            return control_device.QueryRandomAddressM(DeviceShort(addr))
        return control_gear.QueryRandomAddressM(GearShort(addr))

    def QueryRandomAddressL(self, addr: int):
        if self._is_dali2:
            return control_device.QueryRandomAddressL(DeviceShort(addr))
        return control_gear.QueryRandomAddressL(GearShort(addr))

    def QueryRandomAddressResponseValue(self, resp) -> int:
        if self._is_dali2:
            # Control device returns NumericResponse where value is int
            return resp.value
        # Control gear returns Response where value is BackwardFrame
        return resp.value.as_integer


class Commissioning:
    def __init__(
        self, driver: WBDALIDriver, old_devices: list[DaliDeviceAddress], dali2: bool = False
    ) -> None:
        self.driver: WBDALIDriver = driver
        self.last_search_addr = SearchAddress()
        self.found_devices: dict[int, int] = {}  # found this run, not in old_devices
        self.old_devices: dict[int, int] = {}  # loaded from previous state
        for dev in old_devices:
            self.old_devices[dev.short] = dev.random
        self.available_addresses: list[int] = []
        self.binary_search_finder = BinarySearchAddressFinder(
            compare_callback=self.compare, set_search_addr_callback=self.set_search_addr
        )
        self._is_dali2 = dali2
        self._cmds = CommandsCompatibilityLayer(dali2)

    def _add_device(self, short_addr: int, rand_addr: int) -> None:
        log.debug("Adding device: short %d, random 0x%06x", short_addr, rand_addr)
        self.found_devices[short_addr] = rand_addr

    def _set_search_addr(self, addr: int):
        log.debug("Setting search address 0x%06x", addr)

        new_addr = SearchAddress.from_int(addr)

        if self.last_search_addr.high != new_addr.high:
            yield self._cmds.SetSearchAddrH(new_addr.high)
        if self.last_search_addr.medium != new_addr.medium:
            yield self._cmds.SetSearchAddrM(new_addr.medium)
        if self.last_search_addr.low != new_addr.low:
            yield self._cmds.SetSearchAddrL(new_addr.low)
        self.last_search_addr = new_addr

    async def set_search_addr(self, addr: int) -> None:
        await asyncio.gather(
            *(self.driver.send(cmd) for cmd in self._set_search_addr(addr)),
        )

    async def compare(self, addr: int) -> bool:
        """
        Perform a compare command with the given address,
        only sending cmd SEARCHADDR if the corresponding address part has changed.

        Returns True if a device responded or framing error occurred, False otherwise.
        """

        r = await asyncio.gather(
            *(self.driver.send(cmd) for cmd in self._set_search_addr(addr)),
            self.driver.send(self._cmds.Compare()),
        )
        return r[-1].value is True

    def _pick_new_short_address(self, found_addr: int) -> Optional[int]:
        if self.available_addresses:
            # find found_addr in old_devices, and assign the same short address if possible
            for short, rand in self.old_devices.items():
                if rand == found_addr:
                    if short in self.available_addresses:
                        self.available_addresses.remove(short)
                        log.info(
                            "Use previously stored short address %d for device at 0x%06x",
                            short,
                            found_addr,
                        )
                        return short

            return self.available_addresses.pop(0)
        return None

    async def _assign_short_address(self, found_addr: int) -> Optional[int]:
        new_addr = self._pick_new_short_address(found_addr)
        if new_addr is not None:
            log.info(
                "Programming short address %d for device at 0x%06x",
                new_addr,
                found_addr,
            )
            await self.driver.send(self._cmds.ProgramShortAddress(new_addr))
            await asyncio.sleep(0.3)  # Wait for flash write
            r = await self.driver.send(self._cmds.VerifyShortAddress(new_addr))
            if r.value is True:
                log.info("Short address %d programmed successfully", new_addr)
                return new_addr

            log.warning(
                "No answer to VERIFY SHORT ADDRESS %d, try QUERY SHORT ADDRESS instead",
                new_addr,
            )
            r = await self.driver.send(self._cmds.QueryShortAddress())
            short_addr = self._cmds.QueryShortAddressResponseValue(r)
            if short_addr == new_addr:
                log.info(
                    "Short address %d programmed successfully (QUERY SHORT ADDRESS)",
                    new_addr,
                )
                return new_addr

            log.error(
                "Failed to program short address %d for device at 0x%06x",
                new_addr,
                found_addr,
            )

        else:
            log.warning("Device found but no short addresses available")
        return None

    async def find_next_device(self, low: int, high: int) -> Optional[int]:
        return await self.binary_search_finder.find_next_device(low, high)

    async def _randomise_by_short(self, short_addr: Optional[int]) -> None:
        """Randomise the devices with the given short address."""
        log.info(
            "Randomising devices with short address %s",
            "MASK" if short_addr is None else short_addr,
        )
        # "RANDOMISE" accepted by all initialized devices, even the ones which are already withdrawn
        # that's why we'll need to initialise only unaddressed ones
        await self.driver.send(self._cmds.Terminate())
        await self.driver.send(self._cmds.Initialise(short_addr))
        await self.driver.send(self._cmds.Randomise())
        await asyncio.sleep(0.1)  # 100ms per 62386-102-2022 11.7.4

    async def _process_found_device(
        self, found_addr: int, query_short_resp: Union[NumericResponseMask, NumericResponse]
    ) -> set[int]:
        """Returns empty set if no random address conflict,
        or set of short addresses that need to be randomised
        """

        random_address_conflicts = set()
        short_addr = self._cmds.QueryShortAddressResponseValue(query_short_resp)
        log.info(
            "Device found at 0x%06x with short address %s",
            found_addr,
            (query_short_resp.value if short_addr is None else short_addr),
        )

        if query_short_resp.raw_value is None:
            log.error(
                "No response while reading short address of device at 0x%06x. "
                "Maybe the device doesn't implement the QUERY SHORT ADDRESS command? "
                "Ask it to withdraw anyway",
                found_addr,
            )
        elif query_short_resp.raw_value.error:
            log.warning(
                "Framing error while reading short address for device at random address 0x%06x. "
                "Multiple devices share the same random address!",
                found_addr,
            )

            log.info(
                "Mark 0x%06x for readdressing by resetting its short address",
                found_addr,
            )
            await self.set_search_addr(found_addr)
            await self.driver.send(self._cmds.ProgramShortAddress(MASK))
            random_address_conflicts.add(
                None
            )  # None means "unset short address", so we can use it to mark devices with unset short address

        elif short_addr == MASK:
            if found_addr == 0xFFFFFF:
                log.info(
                    "Device with unset random address (0x%06x) and with unset short address. "
                    "Mark it for readdressing (leave short address unset)",
                    found_addr,
                )
                random_address_conflicts.add(None)
            else:
                log.warning(
                    "Device found at 0x%06x, with unset short address. "
                    "Assigning new short address from available addresses",
                    found_addr,
                )
                await self.set_search_addr(found_addr)
                new_short_addr = await self._assign_short_address(found_addr)
                if new_short_addr is not None:
                    self._add_device(new_short_addr, found_addr)
                # await self.driver.send(Withdraw())
        else:
            if short_addr in self.found_devices:
                await self.set_search_addr(found_addr)
                if found_addr == 0xFFFFFF:
                    log.warning(
                        "Device found with unset random address (0x%06x) and with short address %d, "
                        "which is already assigned to another device.",
                        found_addr,
                        short_addr,
                    )
                    log.info(
                        "Mark 0x%06x for readdressing by resetting its short address",
                        found_addr,
                    )
                    await self.driver.send(self._cmds.ProgramShortAddress(MASK))
                    random_address_conflicts.add(None)
                else:
                    log.warning(
                        "Device found at 0x%06x with short address %d, "
                        "which is already assigned to another device. Reassigning short address",
                        found_addr,
                        short_addr,
                    )

                    # устройство точно не получит адрес, который есть у устройств, которые
                    # ещё не были найдены, потому что по результатам сканирования всех коротких адресов
                    # мы уже вычеркнули те короткие адреса, на которые кто-то отвечал
                    new_short_addr = await self._assign_short_address(found_addr)
                    # TODO: что если не удалось запрограммировать новый короткий адрес?
                    self._add_device(new_short_addr, found_addr)
            else:
                if found_addr == 0xFFFFFF:
                    # тут надо понимать, что мы не можем точно различить два устройства
                    # с одним random address по ответу на QUERY SHORT ADDRESS, потому что
                    # они могли ответить одновременно и frame error не было.
                    # поэтому мы всё равно должны сделать RANDOMISE устройствам с пустым random address,
                    # потому что их может быть несколько с этим short address
                    log.info(
                        "Mark 0x%06x (short address %d) for readdressing (randomising)",
                        found_addr,
                        short_addr,
                    )
                    random_address_conflicts.add(short_addr)
                else:
                    log.info(
                        "Keep short address %d for device at 0x%06x",
                        short_addr,
                        found_addr,
                    )
                    self._add_device(short_addr, found_addr)

        return random_address_conflicts

    async def smart_extend(self) -> CommissioningResult:  # pylint: disable=R0912 disable=R0914 disable=R0915
        # Есть весёлая железка, с таким поведением:
        #   на запросы QUERY RANDOM ADDRESS H/M/L не отвечает вообще
        #   на VERIFY SHORT ADDRESS тоже не отвечает
        #   randomAddress у неё всегда 0x14d1d4, на Randomise не реагирует

        short_addr_present = await self._get_present_short_addresses()

        rand_addresses = await asyncio.gather(*[self._get_random_address(x) for x in short_addr_present])

        rand_address_errors: list[int] = []
        known_rand_addrs: list[tuple[int, int]] = []
        rand_addr_frequency: dict[int, int] = {}
        for short, addr in zip(short_addr_present, rand_addresses):
            if addr is None:
                log.error("Failed to get random address for device %d", short)
                rand_address_errors.append(short)
            else:
                log.info("Device %d has random address 0x%06x", short, addr)
                known_rand_addrs.append((short, addr))
                rand_addr_frequency[addr] = rand_addr_frequency.get(addr, 0) + 1

        log.info("known_rand_addrs prior to commissioning: %s", known_rand_addrs)

        log.info("Checking if multiple devices share the same random address")
        short_sent_randomise: list[int] = []
        for short, rand in known_rand_addrs.copy():
            freq = rand_addr_frequency.get(rand, 0)
            if freq > 1:
                log.warning(
                    "Multiple devices share the same random address 0x%06x (%d times) "
                    "as device with short addr %d. Send Randomise",
                    rand,
                    freq,
                    short,
                )
            elif rand == 0xFFFFFF:
                log.warning(
                    "Device with short addr %d has unset random address (0xffffff). Send Randomise",
                    short,
                )
            else:
                continue

            short_sent_randomise.append(short)
            known_rand_addrs.remove((short, rand))
            await self.driver.send(self._cmds.Terminate())
            await self.driver.send(self._cmds.Initialise(short))
            await self.driver.send(self._cmds.Randomise())

        if short_sent_randomise:
            await asyncio.sleep(0.1)  # wait for new random addresses to be generated
            log.info("Querying new random addresses for devices that we sent Randomise to")
            rand_addresses = await asyncio.gather(
                *[self._get_random_address(x) for x in short_sent_randomise]
            )
            for short, addr in zip(short_sent_randomise, rand_addresses):
                if addr is None:
                    log.error(
                        "Failed to get random address for device %d after Randomise",
                        short,
                    )
                else:
                    log.info("Device %d has NEW random address 0x%06x", short, addr)
                    known_rand_addrs.append((short, addr))

        await asyncio.gather(
            self.driver.send(self._cmds.Terminate()), self.driver.send(self._cmds.Initialise(None))
        )

        self.available_addresses = list(range(64))
        for short in short_addr_present:
            if short in self.available_addresses:
                self.available_addresses.remove(short)
                log.info(
                    "Short address %d is already present, removing from available addresses",
                    short,
                )

        # now add random_address from old_devices (state file) to the list of known random addresses
        # important! We must start iteration with actually found random addresses and only move to the
        # old ones from the state file after, because those in front will preserve their short addresses
        # Remember, our goal is to always preserve current state if it's correct and consistent
        _found_rand_addrs = {rand for (short, rand) in known_rand_addrs}
        for short, rand in self.old_devices.items():
            if rand not in _found_rand_addrs:
                log.debug(
                    "Adding old device with short %d and random 0x%06x "
                    "to the end of known random addresses list",
                    short,
                    rand,
                )
                known_rand_addrs.append((short, rand))
                _found_rand_addrs.add(rand)

        log.info("Querying and withdrawing known random addresses")
        # known_rand_addrs = known_rand_addrs[:1]
        cmds = []
        query_cmd_indicies = []
        for short, rand_addr in known_rand_addrs:
            # note: number of search cmds can be less than 3, if some of the parts is the same as before
            cmds.extend(list(self._set_search_addr(rand_addr)))
            query_cmd_indicies.append(len(cmds))
            cmds.append(self._cmds.QueryShortAddress())
            cmds.append(self._cmds.Withdraw())

        random_address_conflicts = set()
        responses = await asyncio.gather(*[self.driver.send(cmd) for cmd in cmds])
        for i, (short, rand_addr) in enumerate(known_rand_addrs):
            resp = responses[query_cmd_indicies[i]]  # QueryShortAddress response
            random_address_conflicts |= await self._process_found_device(rand_addr, resp)

        log.info(
            "After querying known random addresses found %d devices "
            "with random address conflict or unset random address",
            len(random_address_conflicts),
        )
        binary_search_counter = 0
        while True:
            binary_search_counter += 1

            # оптимизацию на поиск сброшенный randomAddress не делаем,
            # иначе устройства с unset random address получают короткие адреса быстрее, чем нормальные
            #
            # log.info("Probing whether there are devices with unset random address (0xffffff)")
            # cmds = [*self._set_search_addr(0xffffff), QueryShortAddress(), Withdraw()]
            # resps = await asyncio.gather(*[self.driver.send(cmd) for cmd in cmds])
            # resp = resps[-2]    # QueryShortAddress response
            # if resp.raw_value is not None:
            #     random_address_conflict += await self._process_found_device(0xffffff, resp)
            # # тут конечно мы всегда вызываем и QueryShortAddress и Compare на 0xffffff,
            # # самый частый случай - девайсов нет, и хорошо бы в нём было экономить один вызов
            # # QueryShortAddress. Т.е. если первый Compare в бинарном поиске что-то вернул,
            # # то останавливаться и делать QueryShortAddress, а потом продолжать уже искать дальше,
            # # если ответа не было. Но чот это довольно некрасиво получается,
            # # и ради экономии одного запроса и 200мс не хочется делать

            log.info("Start binary search (%d)", binary_search_counter)
            low = 0
            high = 0xFFFFFF
            while low < high:
                high = 0xFFFFFF
                found_addr = await self.find_next_device(low, high)
                if found_addr is None:
                    log.info("No device found, exiting")
                    break

                resp = await self.driver.send(self._cmds.QueryShortAddress())
                await self.driver.send(self._cmds.Withdraw())
                random_address_conflicts |= await self._process_found_device(found_addr, resp)
                low = found_addr

            if len(random_address_conflicts) == 0:  # it's O(1)!
                log.info(
                    "Addressing complete, no devices with random address conflict "
                    "or unset random address found, exiting"
                )
                break

            log.info(
                "Randomise the devices with random address conflict "
                "or unset random address to generate new random addresses"
            )
            for short in random_address_conflicts:
                await self._randomise_by_short(short)
            random_address_conflicts = set()

        await self.driver.send(self._cmds.Terminate())

        # Classification based purely on old_devices (from file) and found_devices (current scan)
        # Build reverse maps random -> short
        old_rand_to_short = {rand: short for short, rand in self.old_devices.items()}
        found_rand_to_short = {rand: short for short, rand in self.found_devices.items()}

        old_randoms = set(old_rand_to_short.keys())
        found_randoms = set(found_rand_to_short.keys())

        # 1) Unchanged devices: same short AND same random
        res = CommissioningResult()
        for short, rand in self.old_devices.items():
            if short in self.found_devices and self.found_devices[short] == rand:
                res.unchanged.append(DaliDeviceAddress(short, rand))

        # 2) Changed short address: random present both runs but short differs
        for rand in old_randoms & found_randoms:
            old_short = old_rand_to_short[rand]
            new_short = found_rand_to_short[rand]
            if old_short != new_short:
                res.changed.append(ChangedDevice(DaliDeviceAddress(new_short, rand), old_short))

        # 3) Missing devices: random present before, absent now
        missing = sorted(old_randoms - found_randoms)
        for rand in missing:
            res.missing.append(DaliDeviceAddress(old_rand_to_short[rand], rand))

        # 4) New devices: random present now, absent before
        new = sorted(found_randoms - old_randoms)
        for rand in new:
            res.new.append(DaliDeviceAddress(found_rand_to_short[rand], rand))

        if log.isEnabledFor(logging.DEBUG):
            print_commissioning_summary(res)

        return res

    async def _get_random_address(self, int_address: int) -> Optional[int]:
        responses = await asyncio.gather(
            self.driver.send(self._cmds.QueryRandomAddressH(int_address)),
            self.driver.send(self._cmds.QueryRandomAddressM(int_address)),
            self.driver.send(self._cmds.QueryRandomAddressL(int_address)),
        )
        values = []
        for resp in responses:
            try:
                if not resp or not resp.value:
                    log.error("Failed to get random address part for %s - %s", int_address, resp)
                    return None

                values.append(self._cmds.QueryRandomAddressResponseValue(resp))
            except ResponseError as e:
                log.error("Failed to get random address part for %s - %s", int_address, e)
                return None

        return values[0] << 16 | values[1] << 8 | values[2]

    async def _get_present_short_addresses(self) -> list[int]:
        short_addr_present = []
        if self._is_dali2:
            responses = await asyncio.gather(
                *[self.driver.send(control_device.QueryDeviceStatus(DeviceShort(i))) for i in range(64)]
            )
            for i, resp in enumerate(responses):
                if resp and resp.raw_value is not None:
                    short_addr_present.append(i)
                    log.debug("Control device with short addr %d is present", i)
        else:
            responses = await asyncio.gather(
                *[self.driver.send(control_gear.QueryControlGearPresent(GearShort(i))) for i in range(64)]
            )
            for i, resp in enumerate(responses):
                if resp and resp.value:
                    short_addr_present.append(i)
                    log.debug("Control gear with short addr %d is present", i)
        return short_addr_present

    def _print_binary_search_iteration_info(self, random_addr: int, query_short_resp):
        short_addr = self._cmds.QueryShortAddressResponseValue(query_short_resp)
        if query_short_resp.raw_value is None:
            log.error(
                "No response while reading short address of device at 0x%06x. "
                "Maybe the device doesn't implement the QUERY SHORT ADDRESS command?",
                random_addr,
            )
        elif query_short_resp.raw_value.error:
            log.warning(
                "Framing error while reading short address for device at random address 0x%06x. "
                "Multiple devices share the same random address!",
                random_addr,
            )
        elif short_addr == MASK:
            if random_addr == 0xFFFFFF:
                log.info(
                    "Device with unset random address (0x%06x) and with unset short address.",
                    random_addr,
                )
            else:
                log.warning(
                    "Device found at 0x%06x, with unset short address.",
                    random_addr,
                )
        else:
            log.info(
                "Device found at 0x%06x with short address %d",
                random_addr,
                short_addr,
            )

    async def binary_search(self):
        try:
            await self.driver.send_commands([self._cmds.Terminate(), self._cmds.Initialise(MASK)])
            low = 0
            high = 0xFFFFFF
            while low < high:
                high = 0xFFFFFF
                found_addr = await self.find_next_device(low, high)
                if found_addr is None:
                    log.info("No device found, exiting")
                    break

                resp = await self.driver.send_commands(
                    [self._cmds.QueryShortAddress(), self._cmds.Withdraw()]
                )
                self._print_binary_search_iteration_info(found_addr, resp[0])
                low = found_addr
        finally:
            await self.driver.send(self._cmds.Terminate())


async def search_short(driver: WBDALIDriver, dali2: bool) -> None:
    cmds = CommandsCompatibilityLayer(dali2)
    await driver.send_commands(
        [
            cmds.Terminate(),
            control_device.StartQuiescentMode(DeviceBroadcast()),
        ]
    )
    try:
        if dali2:
            mapper = AsyncDeviceInstanceTypeMapper()
            await mapper.async_autodiscover(driver)
            sorted_items = sorted(mapper.mapping.items(), key=lambda x: x[0][0] * 100 + x[0][1])
            for (addr, inst_num), inst_type in sorted_items:
                logging.info("Control device %d, instance %d: type %d", addr, inst_num, inst_type)
        else:
            responses = await driver.send_commands(
                [control_gear.QueryControlGearPresent(GearShort(i)) for i in range(64)]
            )
            for i, resp in enumerate(responses):
                if resp and resp.value:
                    logging.info("Control gear %d", i)
    finally:
        await driver.send(control_device.StopQuiescentMode(DeviceBroadcast()))


async def check_presence(driver: WBDALIDriver, dali2: bool) -> bool:
    """
    Check if there is at least one device on the bus
    """

    cmds = CommandsCompatibilityLayer(dali2)
    try:
        r = await driver.send_commands(
            [
                cmds.Terminate(),
                cmds.Initialise(MASK),
                cmds.SetSearchAddrH(0xFF),
                cmds.SetSearchAddrM(0xFF),
                cmds.SetSearchAddrL(0xFF),
                cmds.Compare(),
            ],
        )
        return r[-1].value is True
    finally:
        await driver.send(cmds.Terminate())


def print_commissioning_summary(res: CommissioningResult) -> None:
    # Output
    log.info("After commissioning summary:")
    for addr in sorted(res.unchanged, key=lambda x: x.short):
        log.info("  Unchanged device: short %d random 0x%06x", addr.short, addr.random)
    for changed_device in sorted(res.changed, key=lambda x: x.new.random):
        log.info(
            "  Changed device:   random 0x%06x old short %d -> new short %d",
            changed_device.new.random,
            changed_device.old_short,
            changed_device.new.short,
        )
    for addr in res.missing:
        log.warning(
            "  Missing device:   random 0x%06x (old short %d)",
            addr.random,
            addr.short,
        )
    for addr in res.new:
        log.info(
            "  New device:       random 0x%06x (short %d)",
            addr.random,
            addr.short,
        )

    log.info(
        "Totals: unchanged=%d changed=%d missing=%d new=%d",
        len(res.unchanged),
        len(res.changed),
        len(res.missing),
        len(res.new),
    )
