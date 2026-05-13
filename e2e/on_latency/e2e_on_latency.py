# pylint: disable=duplicate-code

# Measure MQTT-to-MQTT latency from publishing a value to a DALI device's `/on`
# topic until the script receives the corresponding sporadic-frame MQTT message
# from the monitor gateway. This includes the time for the monitor's bus-monitor
# register to be polled by wb-mqtt-serial and published to MQTT, so it is strictly
# larger than the actual on-bus delay.
# Stand: two gateways. gw1 (target) runs wb-mqtt-dali against an RGBW dimmer; gw2
# (monitor) has its bus 1 wired in parallel to gw1's bus 1 and is used purely as a
# passive bus monitor. The script overrides polling_interval on the DUT bus via
# Editor/SetBus for the duration of the run and restores it on exit.

import argparse
import asyncio
import logging
import random
import sys
from typing import List, Optional

from dali.frame import ForwardFrame
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.mqtt_dali.bus_traffic import BusTrafficItem, BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_client import rpc_call
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbmqtt import make_mqtt_client

EXIT_SUCCESS = 0
DAPC_BUS = 1
LEVEL_MIN = 1
LEVEL_MAX = 254

# Polling interval the test forces on the DUT bus while it runs. We override the configured
# value via Editor/SetBus and restore it on exit. The application controller clamps any value
# below MIN_POLLING_INTERVAL (1.0 s) up to that minimum on purpose, so 1.0 s is the smallest
# tick we can ask for. The /on request races the next poll tick, so the resulting latency
# distribution reflects "time until the next tick" plus the fraction of that tick already
# consumed by an in-flight poll cycle.
TEST_POLLING_INTERVAL = 1.0

# Upper bound for the random inter-iteration delay. Drawing from U(0, MAX_INTER_PUBLISH_DELAY)
# (set equal to TEST_POLLING_INTERVAL) decorrelates the publish phase from the poll tick so
# the latency distribution covers all phases of the tick, not whichever one a fixed delay
# happens to align with.
MAX_INTER_PUBLISH_DELAY = TEST_POLLING_INTERVAL


async def dispatcher_task(mqtt_dispatcher: MQTTDispatcher) -> None:
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        # Graceful shutdown on cancellation.
        pass


def is_target_dapc_frame(item: BusTrafficItem, short_address: int, expected_level: int) -> bool:
    """Return True if the bus traffic item is a DAPC for `short_address` with `expected_level`.

    The matcher rejects:
    - non-bus-sourced items (we only trust sporadic frames from the monitor gateway);
    - non-forward or non-16-bit frames;
    - frames flagged as broken;
    - frames addressed to anything other than the target short address (broadcast/group
      have a different selector encoding and won't match the (short << 1) byte either).

    Standard commands (Off, QueryActualLevel, ...) are also rejected by the address
    byte: their bit 8 is 1, while DAPC has bit 8 = 0. So `Off(GearShort(target))` with
    the same low 8 bits as our level still won't match because its address byte differs.
    """
    if item.request_source != BusTrafficSource.BUS:
        return False
    frame = item.request
    if not isinstance(frame, ForwardFrame):
        return False
    if len(frame) != 16:
        return False
    if frame.error:
        return False
    raw = frame.as_integer
    address_byte = (raw >> 8) & 0xFF
    data_byte = raw & 0xFF
    return address_byte == (short_address << 1) and data_byte == expected_level


def print_histogram(latencies: List[float], n_bins: int = 6, bar_max_width: int = 50) -> None:
    if not latencies:
        return
    lo = min(latencies)
    hi = max(latencies)
    if hi == lo:
        lo -= 0.5
        hi += 0.5
    bin_width = (hi - lo) / n_bins
    bins = [lo + i * bin_width for i in range(n_bins + 1)]
    counts = [0] * n_bins
    for t in latencies:
        idx = int((t - lo) / bin_width)
        if idx == n_bins:
            idx = n_bins - 1
        counts[idx] += 1
    max_count = max(counts) if counts else 0
    print("\nLatency distribution:")
    for i in range(n_bins):
        cnt = counts[i]
        bar_len = int((cnt / max_count) * bar_max_width) if max_count > 0 else 0
        print(f"[{bins[i]:.3f} — {bins[i + 1]:.3f}) | {cnt:3d} | {'█' * bar_len}")


async def resolve_target_device(target_gateway: str, mqtt_dispatcher: MQTTDispatcher) -> tuple[str, int, str]:
    """Return (mqtt_id, short_address, bus_uid) for the first device on bus 1 of `target_gateway`.

    Relies on default device naming (`<prefix> <short>`) and default mqtt_id format
    (`<bus_id>_<short>`). The test must not be run against a stand whose devices have
    been renamed in the UI — see README.
    """
    gateways = await rpc_call("wb-mqtt-dali", "Editor", "GetList", {}, mqtt_dispatcher, timeout=5.0)
    gateway = next((gw for gw in gateways if gw.get("id") == target_gateway), None)
    if gateway is None:
        raise RuntimeError(f"Gateway {target_gateway!r} not found in wb-mqtt-dali Editor/GetList")
    bus = next((b for b in gateway.get("buses", []) if b.get("name") == f"Bus {DAPC_BUS}"), None)
    if bus is None:
        raise RuntimeError(f"Bus {DAPC_BUS} not found on gateway {target_gateway!r}")
    devices = bus.get("devices", [])
    if not devices:
        raise RuntimeError(f"No devices on bus {DAPC_BUS} of gateway {target_gateway!r}")
    device = devices[0]
    name = device.get("name", "")
    try:
        short_address = int(name.rsplit(" ", 1)[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"Cannot parse short address from device name {name!r}. "
            f"The test assumes default device names (e.g. 'DALI 11', 'DALI2 0'); "
            f"a rename was likely applied — see README."
        ) from exc
    bus_uid = bus["id"]
    mqtt_id = f"{bus_uid}_{short_address}"
    return mqtt_id, short_address, bus_uid


async def set_bus_polling_interval(
    bus_uid: str, polling_interval: float, mqtt_dispatcher: MQTTDispatcher
) -> None:
    await rpc_call(
        "wb-mqtt-dali",
        "Editor",
        "SetBus",
        {"busId": bus_uid, "config": {"polling_interval": polling_interval}},
        mqtt_dispatcher,
        timeout=5.0,
    )


async def get_bus_polling_interval(bus_uid: str, mqtt_dispatcher: MQTTDispatcher) -> float:
    info = await rpc_call(
        "wb-mqtt-dali",
        "Editor",
        "GetBus",
        {"busId": bus_uid},
        mqtt_dispatcher,
        timeout=5.0,
    )
    return float(info["config"]["polling_interval"])


async def run_iteration(  # pylint: disable=R0917
    mqtt_dispatcher: MQTTDispatcher,
    driver: WBDALIDriver,
    on_topic: str,
    short_address: int,
    level: int,
) -> Optional[float]:
    """Publish `level` to `on_topic` and wait for the matching DAPC sporadic-frame
    MQTT message from the monitor gateway.

    Returns latency in seconds (MQTT publish to MQTT receive), or None on timeout.
    """
    loop = asyncio.get_running_loop()
    matched_at: asyncio.Future[float] = loop.create_future()

    def on_frame(item: BusTrafficItem) -> None:
        if matched_at.done():
            return
        if is_target_dapc_frame(item, short_address, level):
            matched_at.set_result(loop.time())

    cleanup = driver.bus_traffic.register(on_frame)
    try:
        t1 = loop.time()
        await mqtt_dispatcher.client.publish(on_topic, str(level), qos=1, retain=False)
        try:
            t2 = await asyncio.wait_for(matched_at, 2.0)
        except asyncio.TimeoutError:
            return None
        return t2 - t1
    finally:
        cleanup()


async def main(argv) -> int:  # pylint: disable=too-many-locals, too-many-statements
    parser = argparse.ArgumentParser(
        description=(
            "Measure MQTT-to-MQTT latency from /on publish to the corresponding DAPC "
            "sporadic-frame MQTT message received from the monitor gateway. "
            "Requires wb-mqtt-dali running against the target gateway with one device "
            "commissioned on bus 1, and a parallel monitor gateway with bus 1 wired "
            "to the same DALI line."
        )
    )
    parser.add_argument(
        "--target-gateway",
        dest="target_gateway",
        type=str,
        default="wb-dali_1",
        help="MQTT device id of the DUT gateway (default: wb-dali_1)",
    )
    parser.add_argument(
        "--monitor-gateway",
        dest="monitor_gateway",
        type=str,
        default="wb-dali_2",
        help="MQTT device id of the monitor gateway (default: wb-dali_2)",
    )
    parser.add_argument(
        "--iterations",
        dest="iterations",
        type=int,
        default=200,
        help="Number of /on publishes to perform (default: 200)",
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

    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=args.log_level)
    logging.getLogger("mqtt_client").setLevel(logging.INFO)

    client = make_mqtt_client(DEFAULT_BROKER_URL)
    mqtt_dispatcher = MQTTDispatcher(client)

    async with client:
        dispatcher = asyncio.create_task(dispatcher_task(mqtt_dispatcher))
        try:
            mqtt_id, short_address, bus_uid = await resolve_target_device(
                args.target_gateway, mqtt_dispatcher
            )
            logging.info(
                "Target device: mqtt_id=%s, short_address=%d (gateway=%s)",
                mqtt_id,
                short_address,
                args.target_gateway,
            )

            # The monitor driver is passive: we never call send_commands on it. Reply
            # topics are still subscribed by initialize() and produce noisy "unknown
            # pointer" warnings whenever the DUT publishes its own replies; suppress
            # them by raising the level on the per-driver child logger.
            monitor_logger = logging.getLogger().getChild(f"{args.monitor_gateway}_bus{DAPC_BUS}")
            monitor_logger.setLevel(logging.ERROR)

            driver = WBDALIDriver(
                WBDALIConfig(device_name=args.monitor_gateway, bus=DAPC_BUS),
                mqtt_dispatcher=mqtt_dispatcher,
                logger=logging.getLogger(),
            )
            await driver.initialize()

            original_polling_interval = await get_bus_polling_interval(bus_uid, mqtt_dispatcher)
            logging.info(
                "Overriding polling_interval on bus %s: %.3f s -> %.3f s",
                bus_uid,
                original_polling_interval,
                TEST_POLLING_INTERVAL,
            )
            await set_bus_polling_interval(bus_uid, TEST_POLLING_INTERVAL, mqtt_dispatcher)

            on_topic = f"/devices/{mqtt_id}/controls/dapc/on"
            latencies: List[float] = []
            timeouts = 0
            try:
                for i in range(args.iterations):
                    level = LEVEL_MIN + (i % (LEVEL_MAX - LEVEL_MIN + 1))
                    latency = await run_iteration(mqtt_dispatcher, driver, on_topic, short_address, level)
                    if latency is None:
                        timeouts += 1
                        logging.warning(
                            "Iteration %d/%d level=%d: timed out waiting for frame",
                            i + 1,
                            args.iterations,
                            level,
                        )
                    else:
                        latencies.append(latency)
                        logging.debug(
                            "Iteration %d/%d level=%d latency=%.3f s",
                            i + 1,
                            args.iterations,
                            level,
                            latency,
                        )
                    if i + 1 < args.iterations:
                        await asyncio.sleep(random.uniform(0, MAX_INTER_PUBLISH_DELAY))
            finally:
                logging.info(
                    "Restoring polling_interval on bus %s to %.3f s",
                    bus_uid,
                    original_polling_interval,
                )
                try:
                    await set_bus_polling_interval(bus_uid, original_polling_interval, mqtt_dispatcher)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logging.error("Failed to restore polling_interval on bus %s: %s", bus_uid, exc)
                await driver.deinitialize()

            print_histogram(latencies)
            print(f"\nIterations: {args.iterations}, timeouts: {timeouts}")
        finally:
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
