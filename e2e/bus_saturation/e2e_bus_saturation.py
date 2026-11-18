#!/usr/bin/env python3
# pylint: disable=duplicate-code

# Bus saturation e2e: every bus of one WB-DALI gateway gets one continuous stream of DAPC and
# QueryActualLevel to its gear, and every query must return the level of its DAPC.
# Runs against a real gateway or the emulator. See README.md.

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Puts the checkout root on sys.path when run as a script; pylint does not see the script directory.
import checkout_path  # pylint: disable=import-error,unused-import
from dali.address import GearShort
from dali.gear.general import DAPC, QueryActualLevel
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from e2e.emulator.emulator_setup import (
    BACKUP_SUFFIX,
    DALI_SERVICE,
    SERIAL_CONFIG,
    SERIAL_SERVICE,
    ConfigBackup,
    EmulatorProcess,
    active_services,
    build_serial_config,
    install_serial_config,
    systemctl,
)
from e2e.emulator.installation_profile import load_profile, resolve_profile_path
from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_client import MQTTRPCClient
from wb.mqtt_dali.wbdali import FramePriority, WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import WbGatewayTransmissionError
from wb.mqtt_dali.wbmqtt import make_mqtt_client

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
DALI_CONFIG = Path("/etc/wb-mqtt-dali.conf")
DEFAULT_PAIRS = 500
GATEWAY_READY_TIMEOUT_S = 120.0
FIRST_LEVEL = 128
LAST_LEVEL = 254
# Commands queued ahead of the replies: two rings keep the gateway's ring full.
RINGS_AHEAD = 2
# Once the whole stream is queued, every command left must get its outcome within this time.
DRAIN_TIMEOUT_S = 15.0

BusNumber = int
MISSING_REPLY = "missing reply"


@dataclass
class BusResult:
    elapsed_s: Optional[float] = None
    gaps: collections.Counter = field(default_factory=collections.Counter)


@dataclass(frozen=True)
class SentPair:
    level: int
    tasks: tuple[asyncio.Task, asyncio.Task]


def gear_by_bus(dali_config: dict, gateway_id: str) -> dict[BusNumber, int]:
    """Short address of the first gear on each bus of the gateway."""
    for gateway in dali_config.get("gateways", []):
        if gateway.get("device_id") != gateway_id:
            continue
        gear = {
            number: next((d["short"] for d in bus.get("devices", []) if not d.get("dali2")), None)
            for number, bus in enumerate(gateway.get("buses", []), start=1)
        }
        if None in gear.values():
            raise ValueError(f"{gateway_id}: no gear on bus(es) {[n for n, s in gear.items() if s is None]}")
        return gear
    raise ValueError(f"{gateway_id} is not in the wb-mqtt-dali config")


def pair_level(pair: int) -> int:
    return FIRST_LEVEL + pair % (LAST_LEVEL - FIRST_LEVEL + 1)


def pair_gap(level: int, dapc: object, query: object) -> Optional[str]:
    """Why the query did not return the level of its DAPC, None if it did."""
    for name, response in (("DAPC", dapc), ("query", query)):
        if isinstance(response, (BaseException, WbGatewayTransmissionError)):
            return f"{name} {type(response).__name__}"
    if query.raw_value is None:
        return "no answer"
    if query.value != level:
        return "wrong level"
    return None


async def stream_bus(driver: WBDALIDriver, short: int, pairs: int, started_at: float) -> BusResult:
    """Queues the bus's stream in order, `RINGS_AHEAD` rings ahead of the replies, and checks each pair."""
    loop = asyncio.get_running_loop()
    ahead = asyncio.Semaphore(RINGS_AHEAD * driver.config.queue_size)

    async def send(command) -> object:
        try:
            return await driver.send(command, BusTrafficSource.WB, FramePriority.AUTOMATIC, driver.logger)
        except Exception as error:  # pylint: disable=broad-exception-caught
            return error
        finally:
            ahead.release()

    sent: list[SentPair] = []
    for pair in range(pairs):
        level = pair_level(pair)
        await ahead.acquire()
        dapc = asyncio.create_task(send(DAPC(GearShort(short), level)))
        await ahead.acquire()
        sent.append(SentPair(level, (dapc, asyncio.create_task(send(QueryActualLevel(GearShort(short)))))))

    _, pending = await asyncio.wait([t for p in sent for t in p.tasks], timeout=DRAIN_TIMEOUT_S)
    result = BusResult(elapsed_s=None if pending else loop.time() - started_at)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for pair in sent:
        if any(t in pending for t in pair.tasks):
            result.gaps[MISSING_REPLY] += 1
        elif (gap := pair_gap(pair.level, *(t.result() for t in pair.tasks))) is not None:
            result.gaps[gap] += 1
    return result


async def wait_gateway_ready(dispatcher: MQTTDispatcher, gateway_id: str) -> None:
    """Waits for a live publication of the gateway's bus state by a freshly started wb-mqtt-serial."""
    ready = asyncio.Event()
    topic = f"/devices/{gateway_id}/controls/bus_1_state"

    def handle(message) -> None:
        if not message.retain:
            ready.set()

    await dispatcher.subscribe(topic, handle)
    try:
        await asyncio.wait_for(ready.wait(), GATEWAY_READY_TIMEOUT_S)
    finally:
        await dispatcher.unsubscribe(topic, handle)


async def run_saturation(
    dispatcher: MQTTDispatcher, gateway_id: str, gear: dict[BusNumber, int], pairs: int
) -> dict[BusNumber, BusResult]:
    drivers = {
        bus: WBDALIDriver(
            WBDALIConfig(device_name=gateway_id, bus=bus),
            mqtt_dispatcher=dispatcher,
            logger=logging.getLogger(f"{gateway_id}.bus_{bus}"),
            rpc_client=MQTTRPCClient(),
        )
        for bus in gear
    }
    for driver in drivers.values():
        await driver.initialize()
    try:
        print(f"Streaming {pairs} DAPC + query pairs to each of buses {sorted(gear)} of {gateway_id}")
        started_at = asyncio.get_running_loop().time()
        results = await asyncio.gather(
            *(stream_bus(drivers[bus], gear[bus], pairs, started_at) for bus in gear)
        )
    finally:
        for driver in drivers.values():
            await driver.deinitialize()
    return dict(zip(gear, results))


def _s(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1f} s"


def report(results: dict[BusNumber, BusResult]) -> int:
    for bus, result in results.items():
        verdict = "OK"
        if result.gaps:
            verdict = "FAIL — " + ", ".join(f"{name} {count}" for name, count in result.gaps.most_common())
        print(f"bus {bus}: {_s(result.elapsed_s)}, {verdict}")
    times = [r.elapsed_s for r in results.values()]
    print(f"all buses: {_s(None if None in times else max(times))}")
    return EXIT_FAILURE if any(r.gaps for r in results.values()) else EXIT_SUCCESS


async def on_gateway(dispatcher: MQTTDispatcher, args: argparse.Namespace) -> int:
    gear = gear_by_bus(json.loads(DALI_CONFIG.read_text(encoding="utf-8")), args.gateway)
    were_active = await active_services(DALI_SERVICE)
    await systemctl("stop", DALI_SERVICE)
    try:
        return report(await run_saturation(dispatcher, args.gateway, gear, args.pairs))
    finally:
        if were_active:
            await systemctl("start", *were_active)


async def on_emulator(dispatcher: MQTTDispatcher, args: argparse.Namespace) -> int:
    profile_path = resolve_profile_path(args.profile, PROFILES_DIR)
    profile = load_profile(profile_path)
    gear = gear_by_bus(profile.dali_config, profile.gateway_device_id)
    were_active = await active_services(SERIAL_SERVICE, DALI_SERVICE)
    backup = ConfigBackup([SERIAL_CONFIG])
    backup.take()
    emulator = EmulatorProcess(str(profile_path))
    try:
        await systemctl("stop", DALI_SERVICE, SERIAL_SERVICE)
        await emulator.start()
        install_serial_config(build_serial_config(profile))
        ready = asyncio.create_task(wait_gateway_ready(dispatcher, profile.gateway_device_id))
        await systemctl("start", SERIAL_SERVICE)
        await ready
        return report(await run_saturation(dispatcher, profile.gateway_device_id, gear, args.pairs))
    finally:
        await _teardown(args.keep_emulator, backup, emulator, were_active)


async def _teardown(
    keep_emulator: bool, backup: ConfigBackup, emulator: EmulatorProcess, were_active: list[str]
) -> None:
    if keep_emulator:
        print(f"\nEmulator kept: pid {emulator.pid}; original in {SERIAL_CONFIG}{BACKUP_SUFFIX}")
        return
    try:
        # wb-mqtt-serial must let go of the emulator's pty before the emulator goes, or it floods the journal.
        await systemctl("stop", SERIAL_SERVICE)
    finally:
        try:
            emulator.stop()
        finally:
            backup.restore()
    if were_active:
        await systemctl("start", *were_active)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream DAPC and level queries to every bus of a WB-DALI")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--gateway", help="Real gateway's MQTT device id, e.g. wb-dali_12")
    target.add_argument("--profile", help="Emulator profile: a name in profiles/ or a directory")
    parser.add_argument(
        "--pairs",
        type=int,
        default=DEFAULT_PAIRS,
        help=f"DAPC + query pairs per bus (default: {DEFAULT_PAIRS})",
    )
    parser.add_argument(
        "--keep-emulator", dest="keep_emulator", action="store_true", help="Leave the emulator running"
    )
    parser.add_argument(
        "-d",
        "--debug",
        dest="log_level",
        action="store_const",
        default=logging.INFO,
        const=logging.DEBUG,
        help="Enable debug logging",
    )
    return parser.parse_args(argv[1:])


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level)
    logging.getLogger("mqtt_client").setLevel(logging.INFO)
    client = make_mqtt_client(DEFAULT_BROKER_URL)
    dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher_task = asyncio.create_task(dispatcher.run())
        try:
            if args.gateway:
                return await on_gateway(dispatcher, args)
            return await on_emulator(dispatcher, args)
        finally:
            dispatcher_task.cancel()
            try:
                await dispatcher_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
