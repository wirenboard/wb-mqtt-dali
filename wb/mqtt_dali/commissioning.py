#!/usr/bin/env python3
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dali.address import GearShort
from dali.driver.base import DALIDriver
from dali.exceptions import ResponseError
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
    SetSearchAddrH,
    SetSearchAddrL,
    SetSearchAddrM,
    Terminate,
    VerifyShortAddress,
    Withdraw,
)

log = logging.getLogger("commissioning")


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


class BinarySearchAddressFinder:
    def __init__(self, compare_callback: callable, set_search_addr_callback: callable):
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


async def get_random_address(driver: DALIDriver, addr: int) -> Optional[int]:
    responses = await asyncio.gather(
        driver.send(QueryRandomAddressH(addr)),
        driver.send(QueryRandomAddressM(addr)),
        driver.send(QueryRandomAddressL(addr)),
    )
    values = []
    for resp in responses:
        try:
            if not resp or not resp.value:
                log.error("Failed to get random address part for %s - %s", addr, resp)
                return None

            values.append(resp.value.as_integer)
        except ResponseError as e:
            log.error("Failed to get random address part for %s - %s", addr, e)
            return None

    return values[0] << 16 | values[1] << 8 | values[2]


async def get_present_short_addresses(driver: DALIDriver) -> list[int]:
    responses = await asyncio.gather(*[driver.send(QueryControlGearPresent(GearShort(i))) for i in range(64)])
    short_addr_present = []
    for i, resp in enumerate(responses):
        if resp and resp.value:
            short_addr_present.append(i)
            log.debug("Control gear with short addr %d is present", i)
    return short_addr_present


class Commissioning:
    def __init__(self, driver: DALIDriver, state_file: str, load: bool = True):
        self.driver: DALIDriver = driver
        self.state_file: str = state_file
        self.last_search_addr = SearchAddress()
        self.found_devices: dict[int, int] = {}  # found this run, not in old_devices
        self.old_devices: dict[int, int] = {}  # loaded from previous state
        self.known_devices: dict[int, int] = {}  # present in old_devices and confirmed this run
        self.missing_devices = {}  # old short present before, not found now (after change detection)
        self.changed_devices: dict[int, tuple[int, int]] = (
            {}
        )  # devices whose short address changed: old_short -> (new_short, random)
        self.available_addresses: list[int] = []
        self.binary_search_finder = BinarySearchAddressFinder(
            compare_callback=self.compare, set_search_addr_callback=self.set_search_addr
        )
        if not self.state_file:
            log.info("No state file provided: commissioning results will not be persisted")
            return
        if load:
            self._load_results()

    def _load_results(self) -> None:
        if not self.state_file or not os.path.isfile(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries", [])
            loaded = 0
            for e in entries:
                short = e.get("short")
                rand = e.get("random")
                if isinstance(short, int) and isinstance(rand, int):
                    self.old_devices[short] = rand
                    loaded += 1
            log.info(
                "Loaded %d device entries (old devices) from %s",
                loaded,
                self.state_file,
            )
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Failed to load commissioning state from %s: %s", self.state_file, e)

    def _save_results(self) -> None:
        if not self.state_file:
            return
        # Save only known (refreshed) + new (discovered) devices
        combined = {**self.known_devices, **self.found_devices}
        data = {
            "version": 1,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "entries": [
                {"short": s, "random": r, "random_hex": f"0x{r:06x}"} for s, r in sorted(combined.items())
            ],
        }
        tmp_path = self.state_file + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.state_file)
            log.info(
                "Saved %d device entries (known + new) to %s",
                len(combined),
                self.state_file,
            )
        except (OSError, TypeError) as e:
            log.error("Failed to save commissioning state to %s: %s", self.state_file, e)

    def _add_device(self, short_addr: int, rand_addr: int) -> None:
        log.debug("Adding device: short %d, random 0x%06x", short_addr, rand_addr)
        self.found_devices[short_addr] = rand_addr

    def _set_search_addr(self, addr: int) -> None:
        log.debug("Setting search address 0x%06x", addr)

        new_addr = SearchAddress.from_int(addr)

        if self.last_search_addr.high != new_addr.high:
            yield SetSearchAddrH(new_addr.high)
        if self.last_search_addr.medium != new_addr.medium:
            yield SetSearchAddrM(new_addr.medium)
        if self.last_search_addr.low != new_addr.low:
            yield SetSearchAddrL(new_addr.low)

        self.last_search_addr = new_addr

    async def set_search_addr(self, addr: int) -> None:
        await asyncio.gather(
            *(self.driver.send(cmd) for cmd in self._set_search_addr(addr)),
        )

    async def compare(self, addr: int) -> bool:
        """Perform a compare command with the given address, only sending cmd SEARCHADDR if the corresponding address part has changed."""

        r = await asyncio.gather(
            *(self.driver.send(cmd) for cmd in self._set_search_addr(addr)),
            self.driver.send(Compare()),
        )
        return r[-1].value is True

    def _pick_new_short_address(self, found_addr: int) -> Optional[int]:
        if self.available_addresses:
            # find found_addr in old_devices, and assign the same short address if possible
            for short, rand in self.old_devices.items():
                if rand == found_addr:
                    if short in self.available_addresses:
                        self.available_addresses.remove(short)
                        logging.info(
                            "Use previously stored short address %d for device at 0x%06x",
                            short,
                            found_addr,
                        )
                        return short

            return self.available_addresses.pop(0)

    async def _assign_short_address(self, found_addr: int) -> Optional[int]:
        new_addr = self._pick_new_short_address(found_addr)
        if new_addr is not None:
            logging.info(
                "Programming short address %d for device at 0x%06x",
                new_addr,
                found_addr,
            )
            await self.driver.send(ProgramShortAddress(new_addr))
            await asyncio.sleep(0.3)  # Wait for flash write
            r = await self.driver.send(VerifyShortAddress(new_addr))
            if r.value is True:
                logging.info("Short address %d programmed successfully", new_addr)
                return new_addr
            else:
                logging.warning(
                    "No answer to VERIFY SHORT ADDRESS %d, try QUERY SHORT ADDRESS instead",
                    new_addr,
                )
                r = await self.driver.send(QueryShortAddress())
                if r.value == (new_addr << 1) | 1:
                    logging.info(
                        "Short address %d programmed successfully (QUERY SHORT ADDRESS)",
                        new_addr,
                    )
                    return new_addr
                else:
                    logging.error(
                        "Failed to program short address %d for device at 0x%06x",
                        new_addr,
                        found_addr,
                    )

        else:
            logging.warning("Device found but no short addresses available")
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
        # that's why we'll need to initialise only unadressed ones
        await self.driver.send(Terminate())
        await self.driver.send(Initialise(address=short_addr, broadcast=False))
        await self.driver.send(Randomise())
        await asyncio.sleep(0.1)  # 100ms per 62386-102-2022 11.7.4

    async def _process_found_device(self, found_addr: int, query_short_resp) -> set[int]:
        "Returns empty set if no random address conflict, or set of short addresses that need to be randomised"

        random_address_conflicts = set()
        log.info(
            "Device found at 0x%06x with short address %s",
            found_addr,
            (
                query_short_resp.value
                if isinstance(query_short_resp.value, str)
                else query_short_resp.value >> 1
            ),
        )

        if query_short_resp.raw_value is None:
            log.error(
                "No response while reading short address of device at 0x%06x. Maybe shitty device doesn't implement the QUERY SHORT ADDRESS command? Ask it to withdraw anyway",
                found_addr,
            )
        elif query_short_resp.raw_value.error:
            log.warning(
                "Framing error while reading short address for device at random address 0x%06x. Multiple devices share the same random address!",
                found_addr,
            )

            log.info(
                "Mark 0x%06x for readdressing by resetting its short address",
                found_addr,
            )
            await self.set_search_addr(found_addr)
            await self.driver.send(ProgramShortAddress("MASK"))
            random_address_conflicts.add(
                None
            )  # None means "unset short address", so we can use it to mark devices with unset short address

        elif query_short_resp.value == "MASK":
            if found_addr == 0xFFFFFF:
                log.info(
                    "Device with unset random address (0x%06x) and with unset short address. Mark it for readdressing (leave short address unset)",
                    found_addr,
                )
                random_address_conflicts.add(None)
            else:
                log.warning(
                    "Device found at 0x%06x, with unset short address. Assigning new short address from available addresses",
                    found_addr,
                )
                await self.set_search_addr(found_addr)
                new_short_addr = await self._assign_short_address(found_addr)
                self._add_device(new_short_addr, found_addr)
                # await self.driver.send(Withdraw())
        else:
            short_addr = query_short_resp.value >> 1
            if short_addr in self.found_devices or short_addr in self.known_devices:
                await self.set_search_addr(found_addr)
                if found_addr == 0xFFFFFF:
                    log.warning(
                        "Device found with unset random address (0x%06x) and with short address %d, which is already assigned to another device.",
                        found_addr,
                        short_addr,
                    )
                    log.info(
                        "Mark 0x%06x for readdressing by resetting its short address",
                        found_addr,
                    )
                    await self.driver.send(ProgramShortAddress("MASK"))
                    random_address_conflicts.add(None)
                else:
                    log.warning(
                        "Device found at 0x%06x with short address %d, which is already assigned to another device. Reassigning short address",
                        found_addr,
                        short_addr,
                    )

                    # устройство точно не получит адрес, который есть у устройств, которые ещё не были найдены, потому что
                    # по результатам сканирования всех коротких адресов мы уже вычеркнули те короткие адреса, на которые кто-то отвечал
                    new_short_addr = await self._assign_short_address(found_addr)
                    self._add_device(new_short_addr, found_addr)
            else:
                if found_addr == 0xFFFFFF:
                    # тут надо понимать, что мы не можем точно различить два устройства с одним random address по ответу на QUERY SHORT ADDRESS,
                    # потому что они могли ответить одновременно и frame error не было.
                    # поэтому мы всё равно должны сделать RANDOMISE устройствам с пустым random address, потому что их может быть несколько с этим short address
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

    async def smart_extend(self) -> None:
        # Есть весёлая железка, с таким поведением:
        #   на запросы QUERY RANDOM ADDRESS H/M/L не отвечает вообще
        #   на VERIFY SHORT ADDRESS тоже не отвечает
        #   randomAddress у неё всегда 0x14d1d4, на Randomise не реагирует

        short_addr_present = await get_present_short_addresses(self.driver)

        rand_addresses = await asyncio.gather(
            *[get_random_address(self.driver, GearShort(x)) for x in short_addr_present]
        )

        rand_address_errors = []
        known_rand_addrs = []
        rand_addr_frequency = {}
        for short, addr in zip(short_addr_present, rand_addresses):
            if addr is None:
                log.error("Failed to get random address for control gear %d", short)
                rand_address_errors.append(short)
            else:
                log.info("Control gear %d has random address 0x%06x", short, addr)
                known_rand_addrs.append((short, addr))
                rand_addr_frequency[addr] = rand_addr_frequency.get(addr, 0) + 1

        log.info("known_rand_addrs prior to commissioning: %s", known_rand_addrs)

        log.info("Checking if multiple devices share the same random address")
        short_sent_randomise = []
        for short, rand in known_rand_addrs.copy():
            freq = rand_addr_frequency.get(rand, 0)
            if freq > 1:
                log.warning(
                    "Multiple devices share the same random address 0x%06x (%d times) as device with short addr %d. Send Randomise",
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
            await self.driver.send(Terminate())
            await self.driver.send(Initialise(broadcast=False, address=short))
            await self.driver.send(Randomise())

        if short_sent_randomise:
            await asyncio.sleep(0.1)  # wait for new random addresses to be generated
            log.info("Querying new random addresses for devices that we sent Randomise to")
            rand_addresses = await asyncio.gather(
                *[get_random_address(self.driver, GearShort(x)) for x in short_sent_randomise]
            )
            for short, addr in zip(short_sent_randomise, rand_addresses):
                if addr is None:
                    log.error(
                        "Failed to get random address for control gear %d after Randomise",
                        short,
                    )
                else:
                    log.info("Control gear %d has NEW random address 0x%06x", short, addr)
                    known_rand_addrs.append((short, addr))

        await asyncio.gather(self.driver.send(Terminate()), self.driver.send(Initialise(broadcast=True)))

        self.available_addresses = list(range(64))
        for short in short_addr_present:
            if short in self.available_addresses:
                self.available_addresses.remove(short)
                log.info(
                    "Short address %d is already present, removing from available addresses",
                    short,
                )

        # now add random_address from old_devices (state file) to the list of known random addresses
        # important! We must start iteration with actually found random addresses and only move to the old ones
        # from the state file after, because those in front will preserve their short addresses
        # Remember, our goal is to always preserve current state if it's correct and consistent
        _found_rand_addrs = {rand for (short, rand) in known_rand_addrs}
        for short, rand in self.old_devices.items():
            if rand not in _found_rand_addrs:
                log.debug(
                    "Adding old device with short %d and random 0x%06x to the end of known random addresses list",
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
            cmds.append(QueryShortAddress())
            cmds.append(Withdraw())

        random_address_conflicts = set()
        responses = await asyncio.gather(*[self.driver.send(cmd) for cmd in cmds])
        for i, (short, rand_addr) in enumerate(known_rand_addrs):
            resp = responses[query_cmd_indicies[i]]  # QueryShortAddress response
            random_address_conflicts |= await self._process_found_device(rand_addr, resp)

        logging.info(
            "After querying known random addresses found %d devices with random address conflict or unset random address",
            len(random_address_conflicts),
        )
        binary_search_counter = 0
        while True:
            binary_search_counter += 1

            # оптимизацию на поиск сброшенный randomAddress не делаем, иначе устройства с unset random address получают короткие адреса быстрее,
            # чем нормальные
            #
            # logging.info("Probing whether there are devices with unset random address (0xffffff)")
            # cmds = [*self._set_search_addr(0xffffff), QueryShortAddress(), Withdraw()]
            # resps = await asyncio.gather(*[self.driver.send(cmd) for cmd in cmds])
            # resp = resps[-2]    # QueryShortAddress response
            # if resp.raw_value is not None:
            #     random_address_conflict += await self._process_found_device(0xffffff, resp)
            # # тут конечно мы всегда вызываем и QueryShortAddress и Compare на 0xffffff, самый частый случай - девайсов нет,
            # # и хорошо бы в нём было экономить один вызов QueryShortAddress. Т.е. если первый Compare в бинарном поиске что-то вернул,
            # # то останавливаться и делать QueryShortAddress, а потом продолжать уже искать дальше, если ответа не было. Но чот это довольно некрасиво получается,
            # # и ради экономии одного запроса и 200мс не хочется делать

            logging.info("Start binary search (%d)", binary_search_counter)
            low = 0
            high = 0xFFFFFF
            while low < high:
                high = 0xFFFFFF
                found_addr = await self.find_next_device(low, high)
                if found_addr is None:
                    log.info("No device found, exiting")
                    break

                resp = await self.driver.send(QueryShortAddress())
                await self.driver.send(Withdraw())
                random_address_conflicts |= await self._process_found_device(found_addr, resp)
                low = found_addr

            if len(random_address_conflicts) == 0:  # it's O(1)!
                log.info(
                    "Addressing complete, no devices with random address conflict or unset random address found, exiting"
                )
                break

            log.info(
                "Randomise the devices  with random address conflict or unset random address to generate new random addresses"
            )
            for short in random_address_conflicts:
                await self._randomise_by_short(short)
            random_address_conflicts = set()

        await self.driver.send(Terminate())

        log.info("After commissioning summary:")

        # Classification based purely on old_devices (from file) and found_devices (current scan)
        # Build reverse maps random -> short
        old_rand_to_short = {rand: short for short, rand in self.old_devices.items()}
        found_rand_to_short = {rand: short for short, rand in self.found_devices.items()}

        old_randoms = set(old_rand_to_short.keys())
        found_randoms = set(found_rand_to_short.keys())

        # 1) Unchanged devices: same short AND same random
        unchanged = []
        for short, rand in self.old_devices.items():
            if short in self.found_devices and self.found_devices[short] == rand:
                unchanged.append((short, rand))

        # 2) Changed short address: random present both runs but short differs
        changed = []
        for rand in old_randoms & found_randoms:
            old_short = old_rand_to_short[rand]
            new_short = found_rand_to_short[rand]
            if old_short != new_short:
                changed.append((rand, old_short, new_short))

        # 3) Missing devices: random present before, absent now
        missing = sorted(old_randoms - found_randoms)

        # 4) New devices: random present now, absent before
        new = sorted(found_randoms - old_randoms)

        # Output
        for short, rand in sorted(unchanged):
            log.info("  Unchanged device: short %d random 0x%06x", short, rand)
        for rand, old_short, new_short in sorted(changed):
            log.info(
                "  Changed device:   random 0x%06x old short %d -> new short %d",
                rand,
                old_short,
                new_short,
            )
        for rand in missing:
            log.warning(
                "  Missing device:   random 0x%06x (old short %d)",
                rand,
                old_rand_to_short[rand],
            )
        for rand in new:
            log.info(
                "  New device:       random 0x%06x (short %d)",
                rand,
                found_rand_to_short[rand],
            )

        log.info(
            "Totals: unchanged=%d changed=%d missing=%d new=%d",
            len(unchanged),
            len(changed),
            len(missing),
            len(new),
        )

        self._save_results()
