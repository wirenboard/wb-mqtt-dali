# Measure how much faster commissioning runs in parallel across the 3 DALI buses
# of a single wb-mqtt-dali gateway, compared to a sequential reference pass.
# wb-mqtt-dali is stopped for the duration of the test and restarted at the end;
# we talk to wb-mqtt-serial directly via WBDALIDriver + Commissioning, same
# pattern as e2e/commissioning/e2e_commissioning.py.

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.mqtt_dali.commissioning import (
    Commissioning,
    print_commissioning_summary,
    search_short,
)
from wb.mqtt_dali.dali2_compat import Dali2CommandsCompatibilityLayer
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbmqtt import make_mqtt_client

EXIT_SUCCESS = 0
EXIT_FAILURE = 1

BUSES = (1, 2, 3)
MAX_CONSECUTIVE_FULL_FAILURES = 3


class IterationOutcome(Enum):
    OK = "ok"
    MISMATCH = "mismatch"
    EXCEPTION = "exception"


class VerifyResetOutcome(Enum):
    OK = "ok"  # reset succeeded — no leftover addresses
    LEFTOVERS = "leftovers"  # verification ran cleanly but found responding devices
    UNAVAILABLE = "unavailable"  # verification couldn't run (e.g. bus lost power)


@dataclass
class BusStats:
    """Per-bus stats. `reference_time`/`reference_device_count` are None when the
    bus failed its reference pass (reset or commission) and is excluded from the
    parallel iterations. `skip_reason` carries a short label for the report."""

    bus: int
    reference_time: Optional[float] = None
    reference_device_count: Optional[int] = None
    skip_reason: Optional[str] = None
    successful_times: List[float] = field(default_factory=list)
    mismatch_count: int = 0
    exception_count: int = 0

    @property
    def has_reference(self) -> bool:
        return self.reference_time is not None and self.reference_device_count is not None


@dataclass
class IterationResult:
    """Per-bus result of one parallel iteration. `elapsed` is None when the
    iteration failed (exception or device-count mismatch) — only successful
    elapsed times feed the histogram."""

    bus: int
    outcome: IterationOutcome
    elapsed: Optional[float]
    device_count: int


async def dispatcher(mqtt_dispatcher: MQTTDispatcher) -> None:
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        pass


async def run_cmd(cmd: str) -> None:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    stdout = stdout.decode("utf-8", errors="replace").rstrip()
    stderr = stderr.decode("utf-8", errors="replace").rstrip()
    print(f'Command: "{cmd}" exit code: {proc.returncode}')
    if stdout:
        print("STDOUT:")
        print(stdout)
    if stderr:
        print("STDERR:")
        print(stderr)


async def reset_bus(driver: WBDALIDriver) -> None:
    """Broadcast Reset + clear short addresses on both DALI-1 and DALI-2 layers.
    Sending both layers covers control gear and control devices without forcing
    the stand to be declared as one or the other."""
    for cmds in (DaliCommandsCompatibilityLayer(), Dali2CommandsCompatibilityLayer()):
        await driver.send(cmds.Reset(None))
        await asyncio.sleep(0.5)
        await driver.send_commands(cmds.setShortAddressCommands(None, 255))


async def verify_reset(driver: WBDALIDriver, bus: int, logger: logging.Logger) -> VerifyResetOutcome:
    """Check both DALI-1 and DALI-2 layers for any gear/device still carrying a short
    address or a random address. A RuntimeError from the underlying driver (e.g. bus
    lost power between reset and verify) is reported as UNAVAILABLE so the caller can
    log a precise reason; genuine programming errors are not caught."""
    try:
        if len(await search_short(driver, dali2=False)) > 0:
            return VerifyResetOutcome.LEFTOVERS
        if len(await search_short(driver, dali2=True)) > 0:
            return VerifyResetOutcome.LEFTOVERS
        random_addresses = await Commissioning(driver, []).binary_search()
    except RuntimeError as exc:
        logger.warning("Bus %d: reset verification could not run: %s", bus, exc)
        return VerifyResetOutcome.UNAVAILABLE
    if not random_addresses:
        return VerifyResetOutcome.OK
    if len(random_addresses) == 1 and random_addresses[0] == 0xFFFFFF:
        return VerifyResetOutcome.OK
    return VerifyResetOutcome.LEFTOVERS


async def commission_bus(driver: WBDALIDriver, print_summary: bool = True) -> tuple[float, int]:
    """Run smart_extend over both layers on the bus — DALI-1 gear first, then DALI-2
    control devices — mirroring the production discovery sequence in
    ApplicationController._do_commissioning. Returns the summed elapsed time and the
    summed new-device count across both layers.
    `print_summary` is suppressed during parallel iterations because per-device log
    lines from concurrent buses would interleave with no bus tag (the module-level
    logger inside commissioning has no per-bus context)."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    total_new = 0
    for dali2 in (False, True):
        res = await Commissioning(driver, [], dali2=dali2).smart_extend()
        if print_summary:
            print_commissioning_summary(res)
        total_new += len(res.new)
    elapsed = loop.time() - start
    return elapsed, total_new


async def reset_and_verify(driver: WBDALIDriver, bus: int, logger: logging.Logger) -> bool:
    await reset_bus(driver)
    outcome = await verify_reset(driver, bus, logger)
    if outcome is VerifyResetOutcome.LEFTOVERS:
        logger.warning("Bus %d: reset failed (devices still respond)", bus)
        return False
    # UNAVAILABLE was already logged inside verify_reset with the specific reason.
    return outcome is VerifyResetOutcome.OK


async def run_reference_pass(drivers: dict[int, WBDALIDriver], logger: logging.Logger) -> dict[int, BusStats]:
    """Run sequential reset+scan on each bus. A bus that fails its reset+verify or
    raises in commission_bus is dropped from the test (skip_reason set, reference
    fields left None); the remaining buses still get a reference. Always returns
    the stats dict — the caller checks whether at least one bus has a reference."""
    stats: dict[int, BusStats] = {bus: BusStats(bus=bus) for bus in drivers}
    for bus, driver in drivers.items():
        logger.info("Reference run: bus %d", bus)
        if not await reset_and_verify(driver, bus, logger):
            stats[bus].skip_reason = "reset failed"
            continue
        try:
            elapsed, device_count = await commission_bus(driver)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Reference run failed on bus %d: %s", bus, exc)
            stats[bus].skip_reason = "commission raised"
            continue
        stats[bus].reference_time = elapsed
        stats[bus].reference_device_count = device_count
        logger.info("Bus %d reference: %.3f s, %d devices", bus, elapsed, device_count)
    return stats


async def parallel_iteration(
    drivers: dict[int, WBDALIDriver],
    expected_device_counts: dict[int, int],
    logger: logging.Logger,
) -> List[IterationResult]:
    """Reset the given buses, then run smart_extend on all of them concurrently.
    Callers pass only participating buses (those with a valid reference) and the
    matching `expected_device_counts` map. Returns one IterationResult per bus.
    Independent: an exception on one bus doesn't affect the others' results."""
    reset_ok = await asyncio.gather(
        *(reset_and_verify(drivers[bus], bus, logger) for bus in drivers),
        return_exceptions=True,
    )

    async def scan_or_skip(bus: int, ok_or_exc) -> IterationResult:
        if isinstance(ok_or_exc, Exception):
            logger.warning("Bus %d: reset raised %s", bus, ok_or_exc)
            return IterationResult(bus=bus, outcome=IterationOutcome.EXCEPTION, elapsed=None, device_count=0)
        if not ok_or_exc:
            return IterationResult(bus=bus, outcome=IterationOutcome.EXCEPTION, elapsed=None, device_count=0)
        try:
            elapsed, device_count = await commission_bus(drivers[bus], print_summary=False)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Bus %d: smart_extend raised %s", bus, exc)
            return IterationResult(bus=bus, outcome=IterationOutcome.EXCEPTION, elapsed=None, device_count=0)
        expected = expected_device_counts[bus]
        if device_count != expected:
            logger.warning(
                "Bus %d: device count mismatch: got %d, expected %d (diff %+d)",
                bus,
                device_count,
                expected,
                device_count - expected,
            )
            return IterationResult(
                bus=bus,
                outcome=IterationOutcome.MISMATCH,
                elapsed=None,
                device_count=device_count,
            )
        return IterationResult(
            bus=bus,
            outcome=IterationOutcome.OK,
            elapsed=elapsed,
            device_count=device_count,
        )

    return await asyncio.gather(*(scan_or_skip(bus, reset_ok[i]) for i, bus in enumerate(drivers)))


def update_stats(stats: dict[int, BusStats], results: List[IterationResult]) -> None:
    for r in results:
        bs = stats[r.bus]
        if r.outcome is IterationOutcome.OK and r.elapsed is not None:
            bs.successful_times.append(r.elapsed)
        elif r.outcome is IterationOutcome.MISMATCH:
            bs.mismatch_count += 1
        else:
            bs.exception_count += 1


def print_histogram(times: List[float], n_bins: int = 6, bar_max_width: int = 50) -> None:
    if not times:
        print("  (no successful iterations)")
        return
    lo = min(times)
    hi = max(times)
    if hi == lo:
        lo -= 0.5
        hi += 0.5
    bin_width = (hi - lo) / n_bins
    bins = [lo + i * bin_width for i in range(n_bins + 1)]
    counts = [0] * n_bins
    for t in times:
        idx = int((t - lo) / bin_width)
        if idx == n_bins:
            idx = n_bins - 1
        counts[idx] += 1
    max_count = max(counts) if counts else 0
    for i in range(n_bins):
        cnt = counts[i]
        bar_len = int((cnt / max_count) * bar_max_width) if max_count > 0 else 0
        print(f"  [{bins[i]:.3f} — {bins[i + 1]:.3f}) | {cnt:3d} | {'█' * bar_len}")


def print_reference_table(stats: dict[int, BusStats]) -> None:
    print("\nReference run (sequential):")
    print("  Bus | Time (s) | Devices")
    print("  ----+----------+--------")
    for bus in sorted(stats):
        bs = stats[bus]
        if bs.has_reference:
            assert bs.reference_time is not None and bs.reference_device_count is not None
            print(f"  {bus:3d} | {bs.reference_time:8.3f} | {bs.reference_device_count:7d}")
        else:
            reason = bs.skip_reason or "no reference"
            print(f"  {bus:3d} | skipped ({reason})")


def print_bus_summary(stats: dict[int, BusStats]) -> None:
    for bus in sorted(stats):
        bs = stats[bus]
        if not bs.has_reference:
            continue
        print(f"\nBus {bus}: successful elapsed-time distribution")
        print_histogram(bs.successful_times)
        print(
            f"  successful={len(bs.successful_times)}, "
            f"mismatched={bs.mismatch_count}, "
            f"exceptions={bs.exception_count}"
        )


async def run_parallel_iterations(
    drivers: dict[int, WBDALIDriver],
    stats: dict[int, BusStats],
    iterations: int,
    logger: logging.Logger,
) -> bool:
    """Run `iterations` parallel iterations over participating buses (those with a
    valid reference), updating `stats`. Returns False if MAX_CONSECUTIVE_FULL_FAILURES
    iterations in a row failed on every participating bus."""
    participating: dict[int, WBDALIDriver] = {}
    expected_device_counts: dict[int, int] = {}
    for bus, driver in drivers.items():
        count = stats[bus].reference_device_count
        if stats[bus].reference_time is None or count is None:
            continue
        participating[bus] = driver
        expected_device_counts[bus] = count
    if not participating:
        logger.error("No participating buses for parallel iterations")
        return False
    logger.info("Parallel iterations participate buses: %s", sorted(participating))
    consecutive_full_failures = 0
    for iteration in range(iterations):
        logger.info("Parallel iteration %d/%d", iteration + 1, iterations)
        results = await parallel_iteration(participating, expected_device_counts, logger)
        update_stats(stats, results)
        all_failed = all(r.outcome is not IterationOutcome.OK for r in results)
        if all_failed:
            consecutive_full_failures += 1
            logger.warning(
                "Iteration %d: all participating buses failed (%d in a row)",
                iteration + 1,
                consecutive_full_failures,
            )
            if consecutive_full_failures >= MAX_CONSECUTIVE_FULL_FAILURES:
                logger.error(
                    "%d consecutive iterations failed on all participating buses, aborting",
                    MAX_CONSECUTIVE_FULL_FAILURES,
                )
                return False
        else:
            consecutive_full_failures = 0
    return True


async def main(argv) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure parallel-vs-sequential commissioning timing across the 3 DALI buses "
            "of a single wb-mqtt-dali gateway. Stops wb-mqtt-dali, then talks to wb-mqtt-serial "
            "directly via WBDALIDriver."
        )
    )
    parser.add_argument(
        "--gateway",
        dest="gateway",
        type=str,
        default="wb-dali_1",
        help="Gateway MQTT device (default: wb-dali_1)",
    )
    parser.add_argument(
        "--iterations",
        dest="iterations",
        type=int,
        default=10,
        help="Number of parallel iterations (default: 10)",
    )
    parser.add_argument(
        "-d",
        "--debug",
        help="Enable debug logging",
        action="store_const",
        const=logging.DEBUG,
        default=logging.INFO,
        dest="log_level",
    )

    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=args.log_level)
    logging.getLogger("mqtt_client").setLevel(logging.INFO)
    logger = logging.getLogger()

    logger.info("Attempting to stop wb-mqtt-dali service...")
    await run_cmd("systemctl stop wb-mqtt-dali")

    try:
        client = make_mqtt_client(DEFAULT_BROKER_URL)
        mqtt_dispatcher = MQTTDispatcher(client)

        async with client:
            dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
            drivers: dict[int, WBDALIDriver] = {}
            try:
                for bus in BUSES:
                    driver = WBDALIDriver(
                        WBDALIConfig(device_name=args.gateway, bus=bus),
                        mqtt_dispatcher=mqtt_dispatcher,
                        logger=logger,
                    )
                    await driver.initialize()
                    drivers[bus] = driver

                stats = await run_reference_pass(drivers, logger)
                if not any(bs.has_reference for bs in stats.values()):
                    logger.error("Reference run failed on every bus, aborting test")
                    print_reference_table(stats)
                    return EXIT_FAILURE

                ok = await run_parallel_iterations(drivers, stats, args.iterations, logger)
                print_reference_table(stats)
                print_bus_summary(stats)
                return EXIT_SUCCESS if ok else EXIT_FAILURE
            finally:
                for driver in drivers.values():
                    try:
                        await driver.deinitialize()
                    except Exception as exc:  # pylint: disable=broad-exception-caught
                        logger.error("deinitialize failed: %s", exc)
                dispatcher_task.cancel()
                try:
                    await dispatcher_task
                except asyncio.CancelledError:
                    pass
    finally:
        logger.info("Restarting wb-mqtt-dali service...")
        await run_cmd("systemctl restart wb-mqtt-dali")


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
