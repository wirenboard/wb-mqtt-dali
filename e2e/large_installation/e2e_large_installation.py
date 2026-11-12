#!/usr/bin/env python3
# pylint: disable=duplicate-code

# Large-installation integration test: real wb-mqtt-serial and wb-mqtt-dali against an emulated
# RS-485 port with a fully populated WB-DALI and its neighbours. Runs on the controller itself;
# configs are replaced for the run and restored on exit. See README.md for the stand and criteria.

from __future__ import annotations

import argparse
import asyncio
import enum
import logging
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Optional

# Puts the checkout root on sys.path when run as a script; pylint does not see the script directory.
import checkout_path  # pylint: disable=import-error,unused-import
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from e2e.large_installation.criteria import (
    ReportedDevice,
    bus_state_violations,
    log_violations,
    violation_classes,
)
from e2e.large_installation.installation_profile import (
    InstallationProfile,
    bus_device_ids,
    dali_config_buses,
    expected_devices,
    load_profile,
    resolve_profile_path,
)
from e2e.large_installation.log_records import (
    DALI_IDENTIFIER,
    SERIAL_IDENTIFIER,
    LogRecord,
)
from e2e.large_installation.rpc_flood import RpcFlood
from e2e.large_installation.stand import (
    BACKUP_SUFFIX,
    DALI_CONFIG,
    DALI_SERVICE,
    SERIAL_CONFIG,
    SERIAL_SERVICE,
    ConfigBackup,
    EmulatorProcess,
    MetaWatcher,
    active_services,
    build_dali_config,
    build_serial_config,
    full_start_time,
    install_stand_files,
    read_journal,
    restarted_services,
    service_restarts,
    systemctl,
)
from e2e.large_installation.startup_traffic import FailedWrite, PortLoadSniffer
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_client import rpc_call
from wb.mqtt_dali.wbmqtt import make_mqtt_client

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
STARTUP_LIMIT_S = 600.0
WAIT_POLL_S = 5.0
# A device whose initialisation failed is initialised by a retry after the full start
STATE_CHECK_DELAY_S = 60.0
RPC_TIMEOUT_S = 30.0
SHOWN_EXAMPLES = 5
DEFAULT_PROFILE = "site"


class Scenario(enum.Enum):
    RPC_FLOOD = "rpc-flood"
    COLD_START = "cold-start"


@dataclass(frozen=True)
class StartupTraffic:
    """Failed wb-mqtt-dali queue writes of an iteration; `t0` is when the services started."""

    t0: float
    failed_writes: list[FailedWrite]


@dataclass
class IterationResult:
    label: str
    startup_s: Optional[float]
    state_violations: list[str]
    log_violations: list[LogRecord]
    startup_over_limit: bool
    restarts: list[str]
    traffic: StartupTraffic

    @property
    def failed(self) -> bool:
        return (
            self.startup_s is None
            or self.startup_over_limit
            or bool(self.state_violations or self.log_violations or self.restarts)
        )


class Run:  # pylint: disable=too-many-instance-attributes
    def __init__(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self,
        args: argparse.Namespace,
        profile: InstallationProfile,
        dispatcher: MQTTDispatcher,
        emulator: EmulatorProcess,
        dali_config: dict,
        flood: Optional[RpcFlood] = None,
    ) -> None:
        self.args = args
        self.flood = flood
        self.sniffer = PortLoadSniffer(profile.gateway_device_id)
        self.profile = profile
        self.dispatcher = dispatcher
        self.emulator = emulator
        self.dali_config = dali_config
        self.devices = expected_devices(dali_config, profile.gateway_device_id)

    async def cold_start(self, label: str) -> IterationResult:
        try:
            t0, full_start_at, services = await self._start()
        finally:
            await self._stop_traffic()
        startup_s = None if full_start_at is None else full_start_at - t0
        if full_start_at is not None:
            await asyncio.sleep(STATE_CHECK_DELAY_S)
        state_violations = await self._check_state()
        records = await read_journal(DALI_IDENTIFIER, t0) + await read_journal(SERIAL_IDENTIFIER, t0)
        limit = self.args.max_startup_time
        return IterationResult(
            label=label,
            startup_s=startup_s,
            state_violations=state_violations,
            log_violations=log_violations(records),
            startup_over_limit=limit is not None and startup_s is not None and startup_s > limit,
            restarts=restarted_services(services, await service_restarts(SERIAL_SERVICE, DALI_SERVICE)),
            traffic=StartupTraffic(t0, list(self.sniffer.failed)),
        )

    # --- Private ---

    async def _start(self) -> tuple[float, Optional[float], dict[str, int]]:
        """Start time, full start time or None, and the restart counts right after the start."""
        self.emulator.check_alive()
        await systemctl("stop", DALI_SERVICE, SERIAL_SERVICE)
        watcher = MetaWatcher()
        await watcher.start(self.dispatcher)
        await self.sniffer.start(self.dispatcher)
        t0 = time.time()
        if self.flood is not None:
            self.flood.start()
        await systemctl("start", SERIAL_SERVICE, DALI_SERVICE)
        services = await service_restarts(SERIAL_SERVICE, DALI_SERVICE)
        try:
            return t0, await self._wait_full_start(watcher), services
        finally:
            await watcher.stop(self.dispatcher)

    async def _stop_traffic(self) -> None:
        if self.flood is not None:
            await self.flood.stop()
        await self.sniffer.stop(self.dispatcher)

    async def _wait_full_start(self, watcher: MetaWatcher) -> Optional[float]:
        """When every bus works and every device is initialised, None past the scenario limit."""
        ids = {d.mqtt_id for d in self.devices} | set(
            bus_device_ids(self.dali_config, self.profile.gateway_device_id)
        )
        deadline = time.monotonic() + STARTUP_LIMIT_S
        while time.monotonic() < deadline:
            if (started_at := full_start_time(watcher.seen, ids)) is not None:
                return started_at
            await asyncio.sleep(WAIT_POLL_S)
        logging.warning(
            "Not started within %.0f s: %s", STARTUP_LIMIT_S, sorted(ids - set(watcher.seen))[:10]
        )
        return None

    async def _check_state(self) -> list[str]:
        """Criterion 1 over all buses: `Editor/GetList`, answered from memory, against the fakes."""
        try:
            gateways = await rpc_call(
                "wb-mqtt-dali", "Editor", "GetList", {}, self.dispatcher, timeout=RPC_TIMEOUT_S
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return [f"state not readable: {exc}"]
        gateway = next((g for g in gateways if g.get("id") == self.profile.gateway_device_id), None)
        if gateway is None:
            return [f"gateway {self.profile.gateway_device_id} missing from Editor/GetList"]
        config_buses = dali_config_buses(self.dali_config, self.profile.gateway_device_id)
        reported_buses = gateway.get("buses", [])
        violations = []
        if len(reported_buses) != len(self.profile.buses):
            violations.append(
                f"{len(reported_buses)} buses in Editor/GetList, {len(self.profile.buses)} in the profile"
            )
        for number, (bus_json, bus, config_bus) in enumerate(
            zip(reported_buses, self.profile.buses, config_buses), start=1
        ):
            reported = [
                ReportedDevice(d.get("name", ""), frozenset(d.get("groups", [])))
                for d in bus_json.get("devices", [])
            ]
            violations += bus_state_violations(
                number,
                reported,
                {g["number"] for g in bus_json.get("groups", [])},
                bus.gear,
                {int(g["number"]) for g in config_bus.get("groups", [])},
                self.devices,
            )
        return violations


def _s(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1f} s"


def print_iteration(result: IterationResult) -> None:
    print(f"\n=== {result.label}: {'FAIL' if result.failed else 'ok'}")
    if result.startup_s is None:
        print("  did not start fully within the scenario limit")
    for restart in result.restarts:
        print(f"  {restart}")
    if result.startup_over_limit:
        print("  full start over --max-startup-time")
    print(f"  criterion 1: {len(result.state_violations)} state mismatches")
    for violation in result.state_violations:
        print(f"    {violation}")
    print(f"  criterion 2: {len(result.log_violations)} warnings or worse")
    for group in violation_classes(result.log_violations):
        print(f"    {group.count:>6} {group.identifier}: {group.message_class[:150]}")
    if result.log_violations:
        print("    first records:")
    for record in result.log_violations[:SHOWN_EXAMPLES]:
        print(f"      {record.identifier} {record.level.name} {record.logger}: {record.message[:160]}")
    print_startup_traffic(result.traffic)
    print(f"  full start: {_s(result.startup_s)}")


def print_startup_traffic(traffic: StartupTraffic) -> None:
    print(f"  failed wb-mqtt-dali queue writes: {len(traffic.failed_writes)}")
    for write in traffic.failed_writes:
        print(
            f"    at +{write.sent_at - traffic.t0:.1f} s {write.target}: "
            f"waited {1000 * write.wait_s:.0f} ms, {write.error[:100]}"
        )


def print_summary(results: list[IterationResult]) -> None:
    starts = [r.startup_s for r in results if r.startup_s is not None]
    print(f"\nIterations: {len(results)}, failed: {sum(1 for r in results if r.failed)}")
    if starts:
        print(
            f"Full start: min {min(starts):.1f} s, median {statistics.median(starts):.1f} s, "
            f"max {max(starts):.1f} s"
        )


async def run_cold_starts(run: Run) -> bool:
    results = []
    for index in range(run.args.iterations):
        result = await run.cold_start(f"{run.args.scenario.value} {index + 1}/{run.args.iterations}")
        results.append(result)
        print_iteration(result)
    print_summary(results)
    return not any(r.failed for r in results)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Large-installation e2e test on an emulated RS-485 port")
    parser.add_argument(
        "--scenario",
        type=Scenario,
        choices=list(Scenario),
        required=True,
        metavar="{" + ",".join(s.value for s in Scenario) + "}",
    )
    parser.add_argument("--iterations", type=int, default=10, help="Iterations (default: 10)")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help=f"Profile (default: {DEFAULT_PROFILE})")
    parser.add_argument(
        "--max-startup-time", dest="max_startup_time", type=float, help="Fail above this full start, s"
    )
    parser.add_argument(
        "--keep-stand", dest="keep_stand", action="store_true", help="Leave the stand running"
    )
    return parser.parse_args(argv[1:])


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("mqtt_client").setLevel(logging.INFO)
    profile = load_profile(args.profile)
    client = make_mqtt_client(DEFAULT_BROKER_URL)
    dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher_task = asyncio.create_task(dispatcher.run())
        were_active = await active_services(SERIAL_SERVICE, DALI_SERVICE)
        backup = ConfigBackup([SERIAL_CONFIG, DALI_CONFIG])
        backup.take()
        emulator = EmulatorProcess(str(resolve_profile_path(args.profile)))
        ok = False
        try:
            dali_config = build_dali_config(profile)
            serial_config = build_serial_config(profile)
            await systemctl("stop", DALI_SERVICE, SERIAL_SERVICE)
            await emulator.start()
            install_stand_files(dali_config, serial_config)
            flood = _rpc_flood(args, profile, dispatcher, serial_config["ports"][0])
            ok = await run_cold_starts(Run(args, profile, dispatcher, emulator, dali_config, flood))
        finally:
            await _teardown(args.keep_stand, backup, emulator, were_active)
            dispatcher_task.cancel()
            try:
                await dispatcher_task
            except asyncio.CancelledError:
                pass
    return EXIT_SUCCESS if ok else EXIT_FAILURE


def _rpc_flood(
    args: argparse.Namespace, profile: InstallationProfile, dispatcher: MQTTDispatcher, port: dict
) -> Optional[RpcFlood]:
    if args.scenario != Scenario.RPC_FLOOD:
        return None
    return RpcFlood(dispatcher, port, profile.gateway_slave_id)


async def _teardown(
    keep_stand: bool, backup: ConfigBackup, emulator: EmulatorProcess, were_active: list[str]
) -> None:
    if keep_stand:
        print(
            f"\nStand kept: emulator pid {emulator.pid}; originals in *{BACKUP_SUFFIX} "
            f"next to {SERIAL_CONFIG} and {DALI_CONFIG}"
        )
        return
    try:
        # wb-mqtt-serial must let go of the emulator's pty before the emulator goes, or it floods the journal.
        await systemctl("stop", DALI_SERVICE, SERIAL_SERVICE)
    finally:
        try:
            emulator.stop()
        finally:
            backup.restore()
    if were_active:
        await systemctl("start", *were_active)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
