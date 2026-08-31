"""Addressing tool for gear that only joins the search after a RANDOMISE sent to all
devices on the bus. That requirement is not in the standard, and the blanket
RANDOMISE re-rolls the random address of every conforming device, so this is kept
out of the service and run separately, on a bus that holds only such gear and with
wb-mqtt-dali stopped.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import IntEnum
from typing import AsyncIterator, Optional

from dali.address import GearShort
from dali.gear import general as control_gear
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from .commissioning import (
    FAILED_SEARCHES_BEFORE_GIVING_UP,
    MAX_RANDOM_ADDRESS,
    MAX_SHORT_ADDRESS,
    RANDOMISE_SETTLE_TIME_S,
    BinarySearchAddressFinder,
    SearchAddressWriter,
)
from .dali_compat import DaliCommandsCompatibilityLayer
from .dali_device import DaliDeviceAddress
from .main import CONFIG_FILEPATH
from .mqtt_dispatcher import MQTTDispatcher
from .wbdali import WBDALIConfig, WBDALIDriver
from .wbdali_utils import (
    FLASH_WRITE_TIME_S,
    MASK,
    NoAnswerError,
    query_response,
    send_with_retry,
)
from .wbmqtt import make_mqtt_client

log = logging.getLogger("address-with-randomise")


class ExitCode(IntEnum):
    SUCCESS = 0
    # The pass stopped before it enumerated the bus; the configuration is left alone.
    ADDRESSING_ABORTED = 1
    # The configuration does not describe the bus: it holds no such gateway or bus, or it could
    # not be read or written.
    CONFIG_INCOMPLETE = 2


@dataclass(frozen=True)
class AddressingOptions:
    gateway: str
    bus: int
    config_path: str = CONFIG_FILEPATH
    search_only: bool = False


class AddressingAborted(RuntimeError):
    """The pass cannot go on without leaving the bus in a state the tool cannot describe."""


async def address_with_randomise(driver: WBDALIDriver, options: AddressingOptions) -> ExitCode:
    """Address the bus, then bring the configuration in line with what the pass found."""
    free_shorts = await free_short_addresses(driver)
    try:
        bus_gear = await address_bus(driver, free_shorts, options.search_only)
    except AddressingAborted as error:
        log.error("%s. The configuration is left untouched", error)
        return ExitCode.ADDRESSING_ABORTED

    if options.search_only:
        return ExitCode.SUCCESS

    if not bus_gear:
        log.warning("The search found no gear, the configuration is left untouched")
        return ExitCode.SUCCESS

    log.info("Addressed short addresses %s", [gear.short for gear in bus_gear])
    return update_config(options, bus_gear)


async def free_short_addresses(driver: WBDALIDriver) -> list[int]:
    """The short addresses nobody answered at, in the order they are handed out."""
    taken = await scan_present_shorts(driver)
    log.info("Short addresses already taken: %s", taken or "none")
    return [short for short in range(MAX_SHORT_ADDRESS + 1) if short not in taken]


async def scan_present_shorts(driver: WBDALIDriver) -> list[int]:
    answers = await asyncio.gather(*(gear_present(driver, short) for short in range(MAX_SHORT_ADDRESS + 1)))
    return [short for short, present in enumerate(answers) if present]


async def gear_present(driver: WBDALIDriver, short: int) -> bool:
    """Whether anything answers at `short`; silence is the negative answer, retries included."""
    try:
        response = await query_response(driver, control_gear.QueryControlGearPresent(GearShort(short)), log)
    except NoAnswerError:
        return False
    return response.value is True


async def address_bus(
    driver: WBDALIDriver, free_shorts: list[int], search_only: bool = False
) -> list[DaliDeviceAddress]:
    """Enumerate the bus after a RANDOMISE addressed to all devices, addressing what it finds.
    With `search_only` the pass reports every device it finds and programs nothing.

    The search comes from `BinarySearchAddressFinder`: it confirms silence on COMPARE with a
    repeat and checks the address below the one the bisection converged on, both of which gear
    that loses single answers needs.
    """
    cmds = DaliCommandsCompatibilityLayer()
    finder = BinarySearchAddressFinder(SearchAddressWriter(driver, cmds))
    await send_with_retry(driver, cmds.Terminate(), log)
    await send_with_retry(driver, cmds.Initialise(MASK), log)
    await send_with_retry(driver, cmds.Randomise(), log)
    await asyncio.sleep(RANDOMISE_SETTLE_TIME_S)
    bus_gear: list[DaliDeviceAddress] = []
    try:
        low = 0
        last_found: Optional[int] = None
        failed_searches = 0
        while low < MAX_RANDOM_ADDRESS:
            found = await finder.find_next_device(low, MAX_RANDOM_ADDRESS)
            if found is None:
                break
            if found in (BinarySearchAddressFinder.UNCONFIRMED_ADDRESS, last_found):
                failed_searches += 1
                if failed_searches >= FAILED_SEARCHES_BEFORE_GIVING_UP:
                    raise AddressingAborted(
                        f"The search above 0x{low:06x} found nothing new "
                        f"{FAILED_SEARCHES_BEFORE_GIVING_UP} times in a row: the gear there does "
                        "not withdraw, and the bus cannot be enumerated without it"
                    )
                continue
            failed_searches = 0
            last_found = found
            if search_only:
                short = await read_short_address(driver, cmds)
                log.info("Gear at 0x%06x, short address %s", found, "none" if short is None else short)
            else:
                bus_gear.append(
                    DaliDeviceAddress(await address_found_gear(driver, cmds, found, free_shorts), found)
                )
            await send_with_retry(driver, cmds.Withdraw(), log)
            low = found
    finally:
        await send_with_retry(driver, cmds.Terminate(), log)
    return bus_gear


async def address_found_gear(
    driver: WBDALIDriver,
    cmds: DaliCommandsCompatibilityLayer,
    found: int,
    free_shorts: list[int],
) -> int:
    """Leave the gear the search selected with a short address: its own, or the lowest free one."""
    short = await read_short_address(driver, cmds)
    if short is not None:
        log.info("Gear at 0x%06x keeps its short address %d", found, short)
        if short in free_shorts:
            # The opening scan missed it; handing the address out would collide with this gear.
            free_shorts.remove(short)
        return short

    if not free_shorts:
        raise AddressingAborted(
            f"Gear at 0x{found:06x} has no short address and all "
            f"{MAX_SHORT_ADDRESS + 1} short addresses are taken"
        )
    new_short = free_shorts.pop(0)
    log.info("Gear at 0x%06x reports no short address, programming %d", found, new_short)
    await send_with_retry(driver, cmds.ProgramShortAddress(new_short), log)
    await asyncio.sleep(FLASH_WRITE_TIME_S)
    if not await short_address_confirmed(driver, cmds, new_short):
        raise AddressingAborted(
            f"Short address {new_short} written to the gear at 0x{found:06x} is confirmed neither "
            "by VERIFY SHORT ADDRESS nor by reading the short address back"
        )
    log.info("Short address %d programmed", new_short)
    return new_short


async def read_short_address(driver: WBDALIDriver, cmds: DaliCommandsCompatibilityLayer) -> Optional[int]:
    """The short address the selected gear reports; `None` when it holds none or stays silent."""
    try:
        response = await query_response(driver, cmds.QueryShortAddress(), log)
    except RuntimeError:
        return None
    short = cmds.QueryShortAddressResponseValue(response)
    return short if short is not None and short <= MAX_SHORT_ADDRESS else None


async def short_address_confirmed(
    driver: WBDALIDriver, cmds: DaliCommandsCompatibilityLayer, short: int
) -> bool:
    response = await query_response(driver, cmds.VerifyShortAddress(short), log)
    if response.value is True:
        return True
    log.warning("No answer to VERIFY SHORT ADDRESS %d, reading the short address back", short)
    return await read_short_address(driver, cmds) == short


def update_config(options: AddressingOptions, bus_gear: list[DaliDeviceAddress]) -> ExitCode:
    try:
        with open(options.config_path, "r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, ValueError) as error:
        log.error("Cannot read %s: %s", options.config_path, error)
        return ExitCode.CONFIG_INCOMPLETE

    entries = bus_entries(config, options)
    if entries is None:
        return ExitCode.CONFIG_INCOMPLETE

    reconcile_entries(entries, bus_gear)
    try:
        write_config(options.config_path, config)
    except OSError as error:
        log.error("Cannot write %s: %s", options.config_path, error)
        return ExitCode.CONFIG_INCOMPLETE

    log.info("%s brought in line with the bus", options.config_path)
    return ExitCode.SUCCESS


def bus_entries(config: dict, options: AddressingOptions) -> Optional[list[dict]]:
    """The device list of the configured bus; `None` when the configuration has no such bus.

    The tool never adds a gateway: a missing one means it was pointed at the wrong configuration.
    """
    for gateway in config.get("gateways", []):
        if gateway.get("device_id") != options.gateway:
            continue
        buses = gateway.get("buses", [])
        if not 1 <= options.bus <= len(buses):
            log.error(
                "Gateway %s in %s has no bus %d, nothing is written",
                options.gateway,
                options.config_path,
                options.bus,
            )
            return None
        return buses[options.bus - 1].setdefault("devices", [])
    log.error("No gateway %s in %s, nothing is written", options.gateway, options.config_path)
    return None


def reconcile_entries(entries: list[dict], bus_gear: list[DaliDeviceAddress]) -> None:
    """Make `entries` describe `bus_gear`, keeping the entries that stay as they are apart from
    their random address: name, mqtt_id and anything else the operator or a later schema put there
    survives the round trip."""
    found = {gear.short: gear for gear in bus_gear}
    kept: list[dict] = []
    removed: list[int] = []
    for entry in entries:
        gear = found.pop(entry.get("short"), None)
        if gear is None:
            removed.append(entry.get("short"))
            continue
        entry["random"] = gear.random
        kept.append(entry)

    added = sorted(found)
    kept.extend({"short": short, "random": found[short].random} for short in added)
    entries[:] = kept

    if added:
        log.info("Added to the configuration: short addresses %s", added)
    if removed:
        log.warning("Removed from the configuration, not found on the bus: short addresses %s", removed)


def write_config(config_path: str, config: dict) -> None:
    target_path = os.path.realpath(config_path)
    temp_fd, temp_path = tempfile.mkstemp(
        prefix="wb-mqtt-dali", suffix=".cfg.tmp", dir=os.path.dirname(target_path)
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
            json.dump(config, temp_file, indent=4)
        os.replace(temp_path, target_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


@asynccontextmanager
async def connected_driver(gateway: str, bus: int) -> AsyncIterator[WBDALIDriver]:
    client = make_mqtt_client(DEFAULT_BROKER_URL)
    mqtt_dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher_task = asyncio.create_task(mqtt_dispatcher.run())
        driver = WBDALIDriver(
            WBDALIConfig(gateway, bus), mqtt_dispatcher=mqtt_dispatcher, logger=logging.getLogger()
        )
        await driver.initialize()
        try:
            yield driver
        finally:
            await driver.deinitialize()
            dispatcher_task.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher_task


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Address DALI control gear that answers the device search only after a RANDOMISE "
            "sent to all devices on the bus. Run it by hand, with wb-mqtt-dali stopped, on a bus "
            "that holds only such gear: RANDOMISE re-rolls the random address of every "
            "conforming device."
        )
    )
    parser.add_argument("--gateway", required=True, help="Gateway MQTT device id, e.g. wb-dali_1")
    parser.add_argument(
        "--bus", type=int, default=1, choices=(1, 2, 3), help="Bus number to address (default: 1)"
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help=(
            "Report what the search finds and program nothing, leaving the configuration "
            "alone. RANDOMISE is still sent, so the random address of every conforming "
            "device is re-rolled."
        ),
    )
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv[1:])


async def main(argv: list[str]) -> ExitCode:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO, format=logging.BASIC_FORMAT, force=True
    )
    logging.getLogger("mqtt_client").setLevel(logging.INFO)

    options = AddressingOptions(args.gateway, args.bus, search_only=args.search_only)
    async with connected_driver(args.gateway, args.bus) as driver:
        return await address_with_randomise(driver, options)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
