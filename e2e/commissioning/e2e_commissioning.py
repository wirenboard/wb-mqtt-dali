#!/usr/bin/env python3
# pylint: disable=C0103

import argparse
import asyncio
import logging
import random
import string
import sys
from urllib.parse import urlparse

import asyncio_mqtt as aiomqtt
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

EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6


async def dispatcher(mqtt_dispatcher: MQTTDispatcher):
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        # Allow graceful shutdown on cancellation; no cleanup needed here.
        pass


def make_mqtt_client(broker_url: str) -> aiomqtt.Client:
    urlparse_result = urlparse(broker_url)
    if urlparse_result.scheme == "unix":
        hostname = urlparse_result.path
        port = 0
    else:
        if urlparse_result.hostname is None:
            raise ValueError("No MQTT hostname specified")
        if urlparse_result.port is None:
            raise ValueError("No MQTT port specified")
        hostname = urlparse_result.hostname
        port = urlparse_result.port
    auth = {}
    if urlparse_result.username:
        auth["username"] = urlparse_result.username
    if urlparse_result.password:
        auth["password"] = urlparse_result.password
    client_id_suffix = "".join(random.sample(string.ascii_letters + string.digits, 8))
    client = aiomqtt.Client(
        client_id=f"wb-mqtt-dali-{client_id_suffix}",
        hostname=hostname,
        port=port,
        transport="websockets" if urlparse_result.scheme == "ws" else urlparse_result.scheme,
        logger=logging.getLogger("mqtt_client"),
        **auth,
    )
    return client


async def run_cmd(cmd: str):
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


async def main(argv):
    parser = argparse.ArgumentParser(
        description="Wiren Board MQTT DALI Bridge E2E commissioning test. Resets DALI devices before test"
    )
    parser.add_argument(
        "--gateway",
        dest="gateway",
        type=str,
        default="wb-dali_1",
        help="Gateway MQTT device (default: wb-dali_1)",
    )

    parser.add_argument(
        "--bus",
        dest="bus",
        type=int,
        default=1,
        help="Bus number to use (default: 1)",
    )

    parser.add_argument(
        "--iterations",
        dest="iterations",
        type=int,
        default=1,
        help="Test iteration count (default: 1)",
    )

    parser.add_argument(
        "--device-count",
        dest="device_count",
        type=int,
        help="Expected number of devices to be commissioned",
    )

    parser.add_argument(
        "--dali2",
        dest="dali2",
        action="store_true",
        help="Use DALI-2 compatible commands (default: False, i.e. use DALI-1 compatible commands)",
    )

    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=logging.INFO)

    logging.info("Attempting to stop wb-mqtt-dali service...")
    await run_cmd("systemctl stop wb-mqtt-dali")

    if args.dali2 is True:
        cmds = Dali2CommandsCompatibilityLayer()
    else:
        cmds = DaliCommandsCompatibilityLayer()

    elapsed_time = []
    successful_runs = []

    for iteration in range(args.iterations):
        logging.info("Starting commissioning iteration %s of %s", iteration + 1, args.iterations)
        client = make_mqtt_client(DEFAULT_BROKER_URL)
        mqtt_dispatcher = MQTTDispatcher(client)
        async with client:
            dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
            driver = WBDALIDriver(
                WBDALIConfig(args.gateway, args.bus),
                mqtt_dispatcher=mqtt_dispatcher,
                logger=logging.getLogger(),
            )
            try:
                await driver.initialize()
                await driver.send(cmds.Reset(None))
                await asyncio.sleep(0.5)  # Wait for devices to reset
                await driver.send_commands(cmds.setShortAddressCommands(None, 255))
                if len(await search_short(driver, args.dali2)):
                    logging.warning("Reset failed, some devices have short address. Iteration failed")
                    successful_runs.append(False)
                    elapsed_time.append(0)
                    continue
                commissioning = Commissioning(driver, [])
                random_addresses = await commissioning.binary_search()
                if len(random_addresses) != 1 or random_addresses[0] != 0xFFFFFF:
                    logging.warning("Reset failed, some devices have random addresses. Iteration failed")
                    successful_runs.append(False)
                    elapsed_time.append(0)
                    continue
                loop = asyncio.get_running_loop()
                start = loop.time()
                res = await commissioning.smart_extend()
                elapsed_time.append(loop.time() - start)
                print_commissioning_summary(res)
                successful_runs.append(res.new == args.device_count)
            finally:
                await driver.deinitialize()
                dispatcher_task.cancel()
                await dispatcher_task

    n_bins = 6
    lo = min(elapsed_time)
    hi = max(elapsed_time)
    if hi == lo:
        lo -= 0.5
        hi += 0.5
    bin_width = (hi - lo) / n_bins
    bins = [lo + i * bin_width for i in range(n_bins + 1)]
    counts = [0] * n_bins
    for t in elapsed_time:
        idx = int((t - lo) / bin_width)
        if idx == n_bins:  # include right edge
            idx = n_bins - 1
        counts[idx] += 1

    max_count = max(counts) if counts else 0
    bar_max_width = 50

    print("\nElapsed time distribution:")
    for i in range(n_bins):
        left = bins[i]
        right = bins[i + 1]
        cnt = counts[i]
        bar_len = int((cnt / max_count) * bar_max_width) if max_count > 0 else 0
        bar = "█" * bar_len
        print(f"[{left:.3f} — {right:.3f}) | {cnt:3d} | {bar}")

    # Statistics
    total = len(elapsed_time)
    mean = sum(elapsed_time) / total
    sorted_times = sorted(elapsed_time)
    mid = total // 2
    if total % 2 == 1:
        median = sorted_times[mid]
    else:
        median = (sorted_times[mid - 1] + sorted_times[mid]) / 2.0
    successes = sum(1 for s in successful_runs if s)
    print(f"\nIterations: {total}, successful: {successes}")
    print(f"Mean time: {mean:.3f} s, median time: {median:.3f} s")
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
