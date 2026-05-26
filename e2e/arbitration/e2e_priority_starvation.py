# pylint: disable=duplicate-code

# Demonstrate DALI multi-master priority-based starvation on a shared bus.
#
# Two gateways are physically wired in parallel on bus 1, with one DALI 1
# gear (an arc-power device) on the bus. The test brings up one
# `WBDALIDriver` per gateway directly over MQTT-RPC to wb-mqtt-serial; the
# `wb-mqtt-dali` daemon should be stopped for the run so its own pollers
# don't add a third party to bus arbitration.
#
# Scenario:
#   1. Set the device's arc-power level via the hammer driver so
#      QueryActualLevel returns a deterministic byte.
#   2. On the long driver: extend `response_timeout` to a large value and
#      fire off a batch of QueryActualLevel commands at priority 4
#      (AUTOMATIC). Do not await.
#   3. Wait until the long driver has received TRIGGER_AFTER responses —
#      at this point the prio-4 batch is uncontested.
#   4. Only then start the hammer driver in a double-buffered prefetch
#      loop (one batch in flight, one already queued — same pattern as
#      `send_command_service` in `wb/mqtt_dali/main.py`), keep sending
#      QueryStatus batches at priority 3 (CONFIGURATION) for
#      HAMMER_DURATION seconds. The hammer's queue never drains.
#   5. After the hammer stops, await the long driver's remaining responses.
#
# Each driver registers a `bus_traffic` callback that records every
# WB-source response with timestamp, gateway label, gateway sequence_id and
# the command/response strings. The full sequence is printed on exit.

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import List

from dali.address import GearShort
from dali.gear.general import DAPC, QueryActualLevel, QueryStatus
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.mqtt_dali.bus_traffic import BusTrafficItem, BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import FramePriority, WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbmqtt import make_mqtt_client

EXIT_SUCCESS = 0
DALI_BUS = 1

LEVEL = 128
LONG_BURST = 16
HAMMER_BATCH = 8
HAMMER_DURATION = 15.0
LONG_RESPONSE_TIMEOUT = 20.0
TRIGGER_AFTER = 2

LONG_DRIVER_PRIORITY = FramePriority.AUTOMATIC
HAMMER_DRIVER_PRIORITY = FramePriority.CONFIGURATION


@dataclass
class TrafficEvent:
    timestamp: float
    driver_name: str
    frame_counter: int
    command_repr: str
    response_repr: str


async def dispatcher_task(mqtt_dispatcher: MQTTDispatcher) -> None:
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        pass


def make_traffic_recorder(events: List[TrafficEvent], driver_name: str):
    """Build a `bus_traffic` callback that appends one `TrafficEvent` per
    WB-source response. BUS-source frames (bus_monitor sporadics) are
    ignored — we only want completions of commands sent from this script.
    """
    loop = asyncio.get_running_loop()

    def callback(item: BusTrafficItem) -> None:
        if item.request_source != BusTrafficSource.WB:
            return
        events.append(
            TrafficEvent(
                timestamp=loop.time(),
                driver_name=driver_name,
                frame_counter=item.frame_counter,
                command_repr=str(item.request),
                response_repr="None" if item.response is None else str(item.response),
            )
        )

    return callback


async def hammer_loop(driver: WBDALIDriver, short_address: int, deadline: float) -> None:
    """Keep the hammer gateway's send queue full until `deadline`.

    Mirrors the double-buffered prefetch in `send_command_service`
    (`wb/mqtt_dali/main.py`): one batch is awaited while the next is
    already in flight, so the gateway never sees an idle moment between
    batches. The hammer keeps issuing prio-3 frames so the long driver's
    prio-4 frames keep losing arbitration on the wire.
    """
    loop = asyncio.get_running_loop()
    cmd_batch = [QueryStatus(GearShort(short_address)) for _ in range(HAMMER_BATCH)]

    def launch() -> asyncio.Task:
        return asyncio.create_task(
            driver.send_commands(cmd_batch, BusTrafficSource.WB, HAMMER_DRIVER_PRIORITY)
        )

    current = launch()
    while True:
        next_task = launch() if loop.time() < deadline else None
        await current
        if next_task is None:
            return
        current = next_task


def print_results(events: List[TrafficEvent], t0: float) -> None:
    if not events:
        print("No events recorded.")
        return
    events_sorted = sorted(events, key=lambda e: e.timestamp)
    print(f"\n=== Sequence of received responses (n={len(events_sorted)}) ===")
    print(f"{'t (s)':>8}  {'driver':>6}  {'fc':>5}  {'cmd':<32}  response")
    print("-" * 90)
    for event in events_sorted:
        cmd = event.command_repr if len(event.command_repr) <= 32 else event.command_repr[:29] + "..."
        print(
            f"{event.timestamp - t0:8.3f}  {event.driver_name:>6}  "
            f"{event.frame_counter:5d}  {cmd:<32}  {event.response_repr}"
        )

    by_driver: dict[str, List[TrafficEvent]] = {}
    for event in events_sorted:
        by_driver.setdefault(event.driver_name, []).append(event)
    print("\n=== Per-driver windows (relative to t0) ===")
    for name, evts in by_driver.items():
        first = evts[0].timestamp - t0
        last = evts[-1].timestamp - t0
        print(f"  {name}: n={len(evts):4d}  window=[{first:7.3f}, {last:7.3f}] s")


async def run_test(  # pylint: disable=too-many-locals
    mqtt_dispatcher: MQTTDispatcher, long_gateway: str, hammer_gateway: str, short_address: int
) -> None:
    logger = logging.getLogger()
    driver_long = WBDALIDriver(
        WBDALIConfig(device_name=long_gateway, bus=DALI_BUS),
        mqtt_dispatcher=mqtt_dispatcher,
        logger=logger,
    )
    driver_hammer = WBDALIDriver(
        WBDALIConfig(device_name=hammer_gateway, bus=DALI_BUS),
        mqtt_dispatcher=mqtt_dispatcher,
        logger=logger,
    )
    await driver_long.initialize()
    try:
        await driver_hammer.initialize()
        try:
            driver_long.response_timeout = LONG_RESPONSE_TIMEOUT
            logging.info(
                "long driver=%s response_timeout=%.1f s; hammer driver=%s",
                long_gateway,
                driver_long.response_timeout,
                hammer_gateway,
            )

            addr = GearShort(short_address)
            logging.info("Setting arc-power level %d on short=%d", LEVEL, short_address)
            await driver_hammer.send_commands(
                [DAPC(addr, LEVEL)], BusTrafficSource.WB, FramePriority.USER_ACTION
            )

            events: List[TrafficEvent] = []
            unregister_long = driver_long.bus_traffic.register(make_traffic_recorder(events, "long"))
            unregister_hammer = driver_hammer.bus_traffic.register(make_traffic_recorder(events, "hammer"))

            trigger_event = asyncio.Event()
            long_completion_count = [0]

            def trigger_callback(item: BusTrafficItem) -> None:
                if item.request_source != BusTrafficSource.WB:
                    return
                long_completion_count[0] += 1
                if long_completion_count[0] >= TRIGGER_AFTER and not trigger_event.is_set():
                    trigger_event.set()

            unregister_trigger = driver_long.bus_traffic.register(trigger_callback)
            try:
                loop = asyncio.get_running_loop()
                t0 = loop.time()
                logging.info(
                    "Launching long batch (%d x QueryActualLevel @ prio %d); hammer will start "
                    "after %d long response(s)",
                    LONG_BURST,
                    LONG_DRIVER_PRIORITY.value,
                    TRIGGER_AFTER,
                )
                long_cmds = [QueryActualLevel(addr) for _ in range(LONG_BURST)]
                long_task = asyncio.create_task(
                    driver_long.send_commands(long_cmds, BusTrafficSource.WB, LONG_DRIVER_PRIORITY)
                )

                await asyncio.wait_for(trigger_event.wait(), timeout=5.0)
                logging.info(
                    "%d long response(s) received at t=%.3f s; starting hammer loop "
                    "(%.1f s of QueryStatus batches size=%d @ prio %d)",
                    TRIGGER_AFTER,
                    loop.time() - t0,
                    HAMMER_DURATION,
                    HAMMER_BATCH,
                    HAMMER_DRIVER_PRIORITY.value,
                )
                hammer_deadline = loop.time() + HAMMER_DURATION
                await hammer_loop(driver_hammer, short_address, hammer_deadline)

                logging.info(
                    "Hammer stopped at t=%.3f s; awaiting long batch (timeout up to %.1f s/cmd)",
                    loop.time() - t0,
                    LONG_RESPONSE_TIMEOUT,
                )
                await long_task
                logging.info("Long batch completed at t=%.3f s", loop.time() - t0)
            finally:
                unregister_long()
                unregister_hammer()
                unregister_trigger()

            print_results(events, t0)
        finally:
            await driver_hammer.deinitialize()
    finally:
        await driver_long.deinitialize()


async def main(argv) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Two gateways share bus 1 with one DALI 1 device. The long-timeout driver "
            f"fires a batch of QueryActualLevel at priority {LONG_DRIVER_PRIORITY.value}; "
            f"the hammer driver keeps the bus busy with priority {HAMMER_DRIVER_PRIORITY.value} "
            f"QueryStatus after the long driver has received {TRIGGER_AFTER} responses."
        )
    )
    parser.add_argument(
        "--long-gateway", default="wb-dali_1", help="MQTT device id of the long-timeout gateway"
    )
    parser.add_argument("--hammer-gateway", default="wb-dali_2", help="MQTT device id of the hammer gateway")
    parser.add_argument(
        "--short-address", type=int, default=0, help="DALI short address of the device (default: 0)"
    )
    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("mqtt_client").setLevel(logging.INFO)

    client = make_mqtt_client(DEFAULT_BROKER_URL)
    mqtt_dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher = asyncio.create_task(dispatcher_task(mqtt_dispatcher))
        try:
            await run_test(mqtt_dispatcher, args.long_gateway, args.hammer_gateway, args.short_address)
        finally:
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
