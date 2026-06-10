# pylint: disable=duplicate-code

# Measure MQTT-to-MQTT latency from injecting a synthetic DALI 2 event frame onto the
# bus via a second gateway until the corresponding short_press<i> MQTT publication is
# received from the DUT. Each iteration sends a burst of N ShortPress events
# (one short_address, instances 0..N-1) at once; the script records per-event arrival
# times and breaks the distribution down by position in the burst, so the share of the
# total latency that comes from DALI half-duplex serialization is visible.
# Stand: two gateways with bus 1 wired in parallel. gw1 (DUT) runs wb-mqtt-dali against
# an RGBW dimmer and has a DALI 2 input-device with N pushbutton instances in its config
# (the physical device does not need to exist — only the in-memory entry in
# _dali2_devices_by_addr is required to decode the injected frames). gw2 (injector) is
# used purely as a transport via its WBDALIDriver. The script overrides
# polling_interval on the DUT bus via Editor/SetBus for the duration of the run and
# restores it on exit.

import argparse
import asyncio
import csv
import json
import logging
import random
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional, Set, Tuple

from dali.address import DeviceShort, InstanceNumber
from dali.command import Command, from_frame
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.device.pushbutton import ShortPress
from dali.device.pushbutton import instance_type as pushbutton_instance_type
from dali.frame import ForwardFrame
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.mqtt_dali.application_controller import format_command
from wb.mqtt_dali.bus_traffic import BusTrafficItem, BusTrafficSource
from wb.mqtt_dali.mqtt_dispatcher import MessageCallback, MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_client import rpc_call
from wb.mqtt_dali.wbdali import FramePriority, WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_error_response import WbGatewayTransmissionError
from wb.mqtt_dali.wbmqtt import make_mqtt_client

EXIT_SUCCESS = 0
DALI_BUS = 1
# Bus number on the injector (or `--monitor-gateway`) that we listen to for the
# `--timing-log`. The stand convention is to physically wire this bus in parallel
# with DALI_BUS, so it observes on-wire traffic on a dedicated UART that doesn't
# compete with the sender.
MONITOR_BUS = 2
EVENT_WAIT_TIMEOUT_S = 2.0

# Default polling interval forced on the DUT bus while the test runs. Same rationale
# as e2e_on_latency: 1.0 s is the minimum the application controller allows, and at
# that rate the RGBW polling cycle eats most of each tick so the event-decode path on
# the DUT is contended with real Modbus traffic. Override with --polling-interval to
# probe other regimes, e.g. a very large value effectively disables polling and
# measures pure event-decode latency without Modbus contention.
DEFAULT_POLLING_INTERVAL = 1.0

# Upper bound for the random inter-iteration delay. Draws from U(0, MAX_INTER_BURST_DELAY)
# decorrelate burst phase from the DUT poll tick at the default 1 Hz poll rate; left
# fixed (does not scale with --polling-interval) so that very large polling values
# don't blow up wall-clock runtime — at large polling intervals there's no tick to
# decorrelate from anyway.
MAX_INTER_BURST_DELAY = 1.0

WB_MQTT_DALI_CONFIG_PATH = Path("/etc/wb-mqtt-dali.conf")
# Suffix appended to the config path for the on-disk backup of the original
# config while the test runs. Presence of this file on script start means the
# previous run was interrupted before restoring the original — refuse to run
# until the operator resolves it manually.
CONFIG_BACKUP_SUFFIX = ".e2e_event_latency.bak"
# Upper bound on how long we wait for wb-mqtt-dali to become responsive again
# after `systemctl restart`. Local restart on the device is normally well under
# 10 s; 30 s is a forgiving cap that still fails fast if the service is broken.
READY_TIMEOUT_S = 30.0
READY_POLL_INTERVAL_S = 0.5
# Upper bound on how long we wait for the DUT DALI 2 input-device to finish
# its first init pass after a wb-mqtt-dali restart — that is the moment
# `_dev_inst_map` gets populated and event frames start decoding as `_Event`.
# Empirically ~8 s for a single missing-device target with retries; 30 s leaves
# plenty of headroom for stands with several phantom DALI-2 devices.
DEVICE_READY_TIMEOUT_S = 30.0


async def dispatcher_task(mqtt_dispatcher: MQTTDispatcher) -> None:
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        # Graceful shutdown on cancellation.
        pass


def print_histogram(latencies: List[float], title: str, n_bins: int = 6, bar_max_width: int = 50) -> None:
    if not latencies:
        print(f"\n{title}: no samples")
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
    print(f"\n{title} (n={len(latencies)}):")
    for i in range(n_bins):
        cnt = counts[i]
        bar_len = int((cnt / max_count) * bar_max_width) if max_count > 0 else 0
        print(f"[{bins[i]:.3f} — {bins[i + 1]:.3f}) | {cnt:3d} | {'█' * bar_len}")


async def resolve_dali2_device(target_gateway: str, mqtt_dispatcher: MQTTDispatcher) -> Tuple[str, int, str]:
    """Return (mqtt_id, short_address, bus_uid) for the first DALI 2 device on bus 1 of `target_gateway`.

    Relies on default device naming (`DALI-2 <short>`) and default mqtt_id format
    (`<bus_uid>_dali2_<short>`). The test must not be run against a stand whose
    DALI 2 device has been renamed in the UI — see README.
    """
    gateways = await rpc_call("wb-mqtt-dali", "Editor", "GetList", {}, mqtt_dispatcher, timeout=5.0)
    gateway = next((gw for gw in gateways if gw.get("id") == target_gateway), None)
    if gateway is None:
        raise RuntimeError(f"Gateway {target_gateway!r} not found in wb-mqtt-dali Editor/GetList")
    bus = next((b for b in gateway.get("buses", []) if b.get("name") == f"Bus {DALI_BUS}"), None)
    if bus is None:
        raise RuntimeError(f"Bus {DALI_BUS} not found on gateway {target_gateway!r}")
    devices = bus.get("devices", [])
    dali2_device = next((d for d in devices if d.get("name", "").startswith("DALI-2 ")), None)
    if dali2_device is None:
        raise RuntimeError(
            f"No DALI 2 device on bus {DALI_BUS} of gateway {target_gateway!r}. "
            f"Stand requires a DALI 2 input-device with pushbutton instances in the config."
        )
    name = dali2_device["name"]
    try:
        short_address = int(name.split(" ", 1)[1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(
            f"Cannot parse short address from device name {name!r}. "
            f"The test assumes default DALI 2 device names ('DALI-2 <short>'); "
            f"a rename was likely applied — see README."
        ) from exc
    bus_uid = bus["id"]
    mqtt_id = f"{bus_uid}_dali2_{short_address}"
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


async def run_cmd(cmd: str) -> None:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace").rstrip()
    stderr = stderr_b.decode("utf-8", errors="replace").rstrip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command {cmd!r} exited with {proc.returncode}.\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )
    logging.debug("Command %r ok. STDOUT: %s STDERR: %s", cmd, stdout, stderr)


def _config_backup_path(config_path: Path) -> Path:
    return config_path.with_suffix(config_path.suffix + CONFIG_BACKUP_SUFFIX)


def evict_injector_from_config(config_path: Path, injector_gateway: str) -> str:
    """Remove the injector gateway from `config_path` and write a backup of the original.

    Returns the original config text so the caller can restore it in `finally`.
    Fails fast if a leftover backup is present (previous run was interrupted) or
    if the injector gateway is not in the config (likely a typo in --injector-gateway).
    """
    backup_path = _config_backup_path(config_path)
    if backup_path.exists():
        raise RuntimeError(
            f"Leftover backup {backup_path} found — a previous run was interrupted before "
            f"the original config was restored. Inspect both files and either restore "
            f"manually (mv {backup_path} {config_path}) or delete the backup if it is stale."
        )
    original_text = config_path.read_text(encoding="utf-8")
    config = json.loads(original_text)
    gateways = config.get("gateways", [])
    if not any(gw.get("device_id") == injector_gateway for gw in gateways):
        raise RuntimeError(
            f"Injector gateway {injector_gateway!r} is not present in {config_path}; "
            f"nothing to evict. Check --injector-gateway."
        )
    backup_path.write_text(original_text, encoding="utf-8")
    config["gateways"] = [gw for gw in gateways if gw.get("device_id") != injector_gateway]
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return original_text


def restore_config(config_path: Path, original_text: str) -> None:
    config_path.write_text(original_text, encoding="utf-8")
    backup_path = _config_backup_path(config_path)
    if backup_path.exists():
        backup_path.unlink()


async def wait_for_gateway_ready(
    target_gateway: str, mqtt_dispatcher: MQTTDispatcher, timeout: float
) -> None:
    """Poll Editor/GetList until `target_gateway` is reported, or `timeout` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error: Optional[str] = None
    while loop.time() < deadline:
        try:
            gateways = await rpc_call("wb-mqtt-dali", "Editor", "GetList", {}, mqtt_dispatcher, timeout=2.0)
            if any(gw.get("id") == target_gateway for gw in gateways):
                return
            last_error = f"target {target_gateway!r} not present in Editor/GetList response"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(READY_POLL_INTERVAL_S)
    raise RuntimeError(f"wb-mqtt-dali did not become ready within {timeout:.1f} s (last error: {last_error})")


@asynccontextmanager
async def dali2_device_ready_subscription(
    mqtt_id: str, mqtt_dispatcher: MQTTDispatcher
) -> AsyncGenerator["asyncio.Future[None]", None]:
    """Subscribe to the DALI 2 device's `/meta` and yield a future that resolves
    on the first fresh (non-retained, non-empty) publication after init.

    `Editor/GetList` becomes responsive as soon as the RPC server is up, which is
    several seconds before `_polling_loop` runs `_initialize_device` for each
    configured device. For DALI 2 input-devices, only the latter call populates
    `_dev_inst_map` (via `_update_dali2_device_instance_map`), and without it
    `from_frame` does not decode event frames as `_Event` and short_press<i> is
    never published. `publish_device` emits `/devices/<mqtt_id>/meta` only after
    `try_initialize_device` succeeds; the broker clears the retain flag on that
    live delivery to an already-subscribed client, so a fresh init shows up as a
    non-retained, non-empty payload. The initial retained snapshot (retain set)
    and the empty `remove_topics_by_driver` clear are both filtered out.

    Crucially this is a context manager so the caller subscribes *before*
    restarting wb-mqtt-dali: on a fast-initialising DUT (e.g. with the injector
    gateway evicted) the post-restart `/meta` publish can fire before a
    subscribe-then-wait helper ever subscribes, and the one-shot signal would be
    missed. Subscribing first makes the wait race-free.
    """
    topic = f"/devices/{mqtt_id}/meta"
    fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def on_message(message) -> None:
        if message.retain:
            return
        if not message.payload:
            return
        if not fut.done():
            fut.set_result(None)

    await mqtt_dispatcher.subscribe(topic, on_message)
    try:
        yield fut
    finally:
        await mqtt_dispatcher.unsubscribe(topic, on_message)


async def restart_wb_mqtt_dali(target_gateway: str, mqtt_dispatcher: MQTTDispatcher) -> None:
    await run_cmd("systemctl restart wb-mqtt-dali")
    await wait_for_gateway_ready(target_gateway, mqtt_dispatcher, READY_TIMEOUT_S)


class BusTimelineLog:
    """Accumulates one row per `BusTrafficItem` from the monitor-bus listener.

    On `close()` sorts rows by `timestamp` and writes them to a CSV with header
    `timestamp,frame_counter,detail`. `frame_counter` is the hardware
    bus_monitor counter from wb-mqtt-serial — gaps mean frames the WB-DALI
    gateway saw on the wire but wb-mqtt-serial didn't read in time.

    The sort on close defends against `BusMonitorFrameHandler`'s late-arrival
    branch: a frame whose counter is in the past relative to expected is
    dispatched (and stamped here) after later-counter frames, so wall-clock
    order does not match counter order. Sorting by `timestamp` preserves the
    actual on-host arrival order regardless of that reorder.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._rows: List[Tuple[float, int, str]] = []

    def record(self, timestamp: float, frame_counter: int, detail: str) -> None:
        self._rows.append((timestamp, frame_counter, detail))

    def close(self) -> None:
        rows = sorted(self._rows, key=lambda r: r[0])
        with self._path.open("w", encoding="utf-8", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(["timestamp", "frame_counter", "detail"])
            for ts, frame_counter, detail in rows:
                writer.writerow([f"{ts:.6f}", str(frame_counter), detail])


def make_event_callback(fut: "asyncio.Future[float]") -> MessageCallback:
    loop = asyncio.get_running_loop()

    def on_message(message) -> None:
        if message.retain:
            return
        if message.payload != b"1":
            return
        if fut.done():
            return
        fut.set_result(loop.time())

    return on_message


@dataclass
class BurstResult:
    """Outcome of a single burst.

    `arrivals[i]` is `None` when MQTT arrival timed out for an instance whose
    frame `send_commands` accepted, or when the instance is listed in
    `transmission_error_instances` (frame never went on the wire — we skip
    the MQTT wait and report the failure separately).
    """

    t1: float
    arrivals: Dict[int, Optional[float]]
    transmission_error_instances: Set[int]


@asynccontextmanager
async def injector_driver_running(
    injector_gateway: str, mqtt_dispatcher: MQTTDispatcher
) -> AsyncGenerator[WBDALIDriver, None]:
    """Construct the injector `WBDALIDriver`, initialise it, and ensure
    `deinitialize` runs on exit even if init or the body raises.
    """
    driver = WBDALIDriver(
        WBDALIConfig(device_name=injector_gateway, bus=DALI_BUS),
        mqtt_dispatcher=mqtt_dispatcher,
        logger=logging.getLogger(),
    )
    try:
        await driver.initialize()
        yield driver
    finally:
        try:
            await driver.deinitialize()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.error("Failed to deinitialize injector driver: %s", exc)


@asynccontextmanager
async def polling_interval_override(
    bus_uid: str, polling_interval: float, mqtt_dispatcher: MQTTDispatcher
) -> AsyncGenerator[None, None]:
    """Override `polling_interval` on the DUT bus for the duration of the body
    and restore the previous value on exit. If the override never took effect
    (read failed before set), no restore is attempted.
    """
    original_polling_interval = await get_bus_polling_interval(bus_uid, mqtt_dispatcher)
    logging.info(
        "Overriding polling_interval on bus %s: %.3f s -> %.3f s",
        bus_uid,
        original_polling_interval,
        polling_interval,
    )
    await set_bus_polling_interval(bus_uid, polling_interval, mqtt_dispatcher)
    try:
        yield
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


@asynccontextmanager
async def bus_monitor_listener(
    timing_log_path: Optional[Path],
    monitor_gateway: str,
    short_address: int,
    burst_size: int,
    mqtt_dispatcher: MQTTDispatcher,
) -> AsyncGenerator[None, None]:
    """Run the optional monitor-bus listener that feeds `--timing-log`.

    No-op when `timing_log_path` is None. Otherwise constructs and
    initialises a second `WBDALIDriver` against `monitor_gateway` bus
    `MONITOR_BUS`, registers a `BusTrafficCallbacks` listener that records
    every dispatched frame into a `BusTimelineLog`, and on exit unregisters
    the listener, deinitialises the driver and writes the log. All three
    cleanup steps run independently — a failure in one is logged but does
    not skip the others.
    """
    if timing_log_path is None:
        yield
        return

    logging.info(
        "Writing bus timeline log to %s (monitor=%s bus %d)",
        timing_log_path,
        monitor_gateway,
        MONITOR_BUS,
    )
    timeline_log = BusTimelineLog(timing_log_path)
    dev_inst_map = DeviceInstanceTypeMapper()
    for i in range(burst_size):
        dev_inst_map.add_type(
            short_address=DeviceShort(short_address),
            instance_number=InstanceNumber(i),
            instance_type=pushbutton_instance_type,
        )
    listener_driver = WBDALIDriver(
        WBDALIConfig(device_name=monitor_gateway, bus=MONITOR_BUS),
        mqtt_dispatcher=mqtt_dispatcher,
        logger=logging.getLogger("monitor_bus"),
        dev_inst_map=dev_inst_map,
    )
    unregister_listener = None
    try:
        await listener_driver.initialize()

        def on_bus_traffic(item: BusTrafficItem) -> None:
            decoded: Optional[Command] = None
            if isinstance(item.request, ForwardFrame):
                try:
                    decoded = from_frame(item.request, dev_inst_map=dev_inst_map)
                except Exception:  # pylint: disable=broad-exception-caught
                    pass
            timeline_log.record(
                time.time(),
                item.frame_counter,
                format_command(item.request, decoded),
            )

        unregister_listener = listener_driver.bus_traffic.register(on_bus_traffic)
        yield
    finally:
        if unregister_listener is not None:
            try:
                unregister_listener()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.error("Failed to unregister listener callback: %s", exc)
        try:
            await listener_driver.deinitialize()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.error("Failed to deinitialize listener driver: %s", exc)
        try:
            timeline_log.close()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.error("Failed to write timeline log: %s", exc)


async def run_burst(
    mqtt_dispatcher: MQTTDispatcher,
    driver: WBDALIDriver,
    mqtt_id: str,
    short_address: int,
    burst_size: int,
) -> BurstResult:
    """Send one burst of N ShortPress events and collect arrival times per instance.

    Subscribes per-burst so each callback closes over only this burst's future:
    a late publication from a previous (timed-out) burst lands on an already-
    unsubscribed callback and cannot resolve the next burst's futures.
    """
    loop = asyncio.get_running_loop()
    arrivals: Dict[int, "asyncio.Future[float]"] = {i: loop.create_future() for i in range(burst_size)}
    topics: Dict[int, str] = {i: f"/devices/{mqtt_id}/controls/short_press{i}" for i in range(burst_size)}
    callbacks: Dict[int, MessageCallback] = {i: make_event_callback(arrivals[i]) for i in range(burst_size)}
    for i in range(burst_size):
        await mqtt_dispatcher.subscribe(topics[i], callbacks[i])
    try:
        events = [
            ShortPress(short_address=DeviceShort(short_address), instance_number=i) for i in range(burst_size)
        ]
        t1 = loop.time()
        # Pushbutton eventPriority defaults to 3 (IEC 62386-301:2017 §9.4.1);
        responses = await driver.send_commands(events, BusTrafficSource.WB, FramePriority.CONFIGURATION)
        transmission_error_instances: Set[int] = {
            i for i, r in enumerate(responses) if isinstance(r, WbGatewayTransmissionError)
        }
        arrival_times: Dict[int, Optional[float]] = {}
        for i in range(burst_size):
            if i in transmission_error_instances:
                arrival_times[i] = None
                continue
            try:
                arrival_times[i] = await asyncio.wait_for(arrivals[i], EVENT_WAIT_TIMEOUT_S)
            except asyncio.TimeoutError:
                arrival_times[i] = None
        return BurstResult(
            t1=t1, arrivals=arrival_times, transmission_error_instances=transmission_error_instances
        )
    finally:
        for i in range(burst_size):
            await mqtt_dispatcher.unsubscribe(topics[i], callbacks[i])


@dataclass
class BurstRunResults:
    per_position_latencies: Dict[int, List[float]] = field(default_factory=dict)
    timeouts: int = 0
    transmission_errors: int = 0
    total_events: int = 0


async def run_iterations(  # pylint: disable=too-many-arguments, R0917
    mqtt_dispatcher: MQTTDispatcher,
    driver: WBDALIDriver,
    mqtt_id: str,
    short_address: int,
    burst_size: int,
    iterations: int,
) -> BurstRunResults:
    results = BurstRunResults(
        per_position_latencies={i: [] for i in range(burst_size)},
    )
    for iteration in range(iterations):
        burst = await run_burst(mqtt_dispatcher, driver, mqtt_id, short_address, burst_size)
        for instance, arrival in burst.arrivals.items():
            results.total_events += 1
            if instance in burst.transmission_error_instances:
                results.transmission_errors += 1
                logging.warning(
                    "Iteration %d/%d instance=%d: send_commands returned transmission error",
                    iteration + 1,
                    iterations,
                    instance,
                )
            elif arrival is None:
                results.timeouts += 1
                logging.warning(
                    "Iteration %d/%d instance=%d: timed out waiting for short_press",
                    iteration + 1,
                    iterations,
                    instance,
                )
            else:
                results.per_position_latencies[instance].append(arrival - burst.t1)
                logging.debug(
                    "Iteration %d/%d instance=%d latency=%.3f s",
                    iteration + 1,
                    iterations,
                    instance,
                    arrival - burst.t1,
                )
        if iteration + 1 < iterations:
            await asyncio.sleep(random.uniform(0, MAX_INTER_BURST_DELAY))
    return results


async def main(argv) -> int:  # pylint: disable=too-many-locals, too-many-statements, too-many-branches
    parser = argparse.ArgumentParser(
        description=(
            "Measure MQTT-to-MQTT latency from injecting DALI 2 ShortPress events on "
            "the bus (via a second gateway) to the corresponding short_press<i> MQTT "
            "publication from the DUT. Each iteration sends a burst of N events with "
            "the same short_address and instances 0..N-1; per-event latency is broken "
            "down by position in the burst."
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
        "--injector-gateway",
        dest="injector_gateway",
        type=str,
        default="wb-dali_2",
        help="MQTT device id of the injector gateway (default: wb-dali_2)",
    )
    parser.add_argument(
        "--burst-size",
        dest="burst_size",
        type=int,
        default=8,
        help="Number of events (instances 0..N-1) in one burst (default: 8)",
    )
    parser.add_argument(
        "--iterations",
        dest="iterations",
        type=int,
        default=200,
        help="Number of bursts to send (default: 200)",
    )
    parser.add_argument(
        "--polling-interval",
        dest="polling_interval",
        type=float,
        default=DEFAULT_POLLING_INTERVAL,
        help=(
            f"polling_interval forced on the DUT bus while the test runs, in seconds "
            f"(default: {DEFAULT_POLLING_INTERVAL}). Pass a very large value to "
            f"effectively disable polling and measure event-decode latency without "
            f"Modbus contention."
        ),
    )
    parser.add_argument(
        "--timing-log",
        dest="timing_log",
        type=Path,
        default=None,
        help=(
            "If set, write a CSV log of bus_traffic from the monitor bus. Columns: "
            "`timestamp`, `frame_counter`, `detail`. One row per frame dispatched by "
            "the listener driver's `BusTrafficCallbacks`; `frame_counter` is the "
            "hardware bus_monitor counter (gaps = frames the gateway saw on the "
            "wire but wb-mqtt-serial didn't read in time). `detail` is formatted by "
            "the same `format_command` used in production for `/wb-dali/<bus>/"
            "bus_monitor`. Rows are sorted by `timestamp` before write."
        ),
    )
    parser.add_argument(
        "--monitor-gateway",
        dest="monitor_gateway",
        type=str,
        default=None,
        help=(
            "MQTT device id of the gateway whose bus_monitor to listen to for the "
            "timing log (default: same as --injector-gateway). The bus number is "
            f"always {MONITOR_BUS} — the stand convention is to physically wire "
            "that bus in parallel to bus 1 (the bus under test) so it captures "
            "on-wire traffic on a dedicated UART that doesn't compete with the "
            "sender."
        ),
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

    if args.burst_size < 1:
        parser.error("--burst-size must be >= 1")

    logging.basicConfig(level=args.log_level)
    logging.getLogger("mqtt_client").setLevel(logging.INFO)

    client = make_mqtt_client(DEFAULT_BROKER_URL)
    mqtt_dispatcher = MQTTDispatcher(client)

    async with client:
        dispatcher = asyncio.create_task(dispatcher_task(mqtt_dispatcher))
        try:
            # Resolve the DALI 2 device while wb-mqtt-dali is still up so we can
            # subscribe to its /meta before the restart and not race the one-shot
            # init publish. Evicting the injector does not touch the target
            # gateway, so mqtt_id stays valid across the restart.
            mqtt_id, short_address, bus_uid = await resolve_dali2_device(args.target_gateway, mqtt_dispatcher)
            logging.info(
                "Target DALI 2 device: mqtt_id=%s, short_address=%d (gateway=%s, bus=%d)",
                mqtt_id,
                short_address,
                args.target_gateway,
                DALI_BUS,
            )

            logging.info(
                "Evicting injector gateway %s from %s and restarting wb-mqtt-dali",
                args.injector_gateway,
                WB_MQTT_DALI_CONFIG_PATH,
            )
            original_config_text = evict_injector_from_config(WB_MQTT_DALI_CONFIG_PATH, args.injector_gateway)
            try:
                async with dali2_device_ready_subscription(mqtt_id, mqtt_dispatcher) as device_ready:
                    await restart_wb_mqtt_dali(args.target_gateway, mqtt_dispatcher)
                    logging.info("Waiting for DALI 2 device init on DUT before starting iterations")
                    try:
                        await asyncio.wait_for(device_ready, DEVICE_READY_TIMEOUT_S)
                    except asyncio.TimeoutError as exc:
                        raise RuntimeError(
                            f"DALI 2 device {mqtt_id!r} did not finish initialization within "
                            f"{DEVICE_READY_TIMEOUT_S:.1f} s (no fresh publication on "
                            f"/devices/{mqtt_id}/meta)"
                        ) from exc

                monitor_gateway = args.monitor_gateway or args.injector_gateway
                async with injector_driver_running(args.injector_gateway, mqtt_dispatcher) as driver:
                    async with polling_interval_override(bus_uid, args.polling_interval, mqtt_dispatcher):
                        async with bus_monitor_listener(
                            args.timing_log,
                            monitor_gateway,
                            short_address,
                            args.burst_size,
                            mqtt_dispatcher,
                        ):
                            results = await run_iterations(
                                mqtt_dispatcher,
                                driver,
                                mqtt_id,
                                short_address,
                                args.burst_size,
                                args.iterations,
                            )

                aggregated = [t for ts in results.per_position_latencies.values() for t in ts]
                print_histogram(aggregated, "Aggregated latency")
                for i in range(args.burst_size):
                    print_histogram(results.per_position_latencies[i], f"Latency at position_in_burst={i}")
                print(
                    f"\nevents: {results.total_events}, "
                    f"timeouts: {results.timeouts}, "
                    f"transmission_errors: {results.transmission_errors}"
                )
            finally:
                logging.info(
                    "Restoring original %s and restarting wb-mqtt-dali",
                    WB_MQTT_DALI_CONFIG_PATH,
                )
                try:
                    restore_config(WB_MQTT_DALI_CONFIG_PATH, original_config_text)
                    await restart_wb_mqtt_dali(args.target_gateway, mqtt_dispatcher)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logging.error(
                        "Failed to restore %s / restart wb-mqtt-dali: %s",
                        WB_MQTT_DALI_CONFIG_PATH,
                        exc,
                    )
        finally:
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
