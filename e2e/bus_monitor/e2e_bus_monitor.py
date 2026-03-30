import argparse
import asyncio
import logging
import random
import signal
import string
import sys
from urllib.parse import urlparse

import asyncio_mqtt as aiomqtt
from dali.address import GearShort
from dali.gear.general import QueryActualLevel
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import FRAME_COUNTER_MODULO
from wb.mqtt_dali.wbdali import WBDALIConfig as WBDALIDriverNewConfig
from wb.mqtt_dali.wbdali import WBDALIDriver as WBDALIDriverNew
from wb.mqtt_dali.bus_traffic import BusTrafficItem


EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6
SEND_BATCH_SIZE = 16


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


async def main(argv):
    parser = argparse.ArgumentParser(
        description="Wiren Board MQTT DALI Bridge E2E bus monitor test. "
        "Expects that bus 1 and bus 2 are connected to each other. "
        "Sends commands to bus2 and listens for them on bus1. "
        "Checks missing and malformed frames on bus1."
    )
    parser.add_argument(
        "--gateway",
        dest="gateway",
        type=str,
        default="wb-dali_1",
        help="Gateway MQTT device (default: wb-dali_1)",
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

    last_frame_counter = None
    missed_frames = 0
    total_frames = 0
    got_frames = 0

    def bus_monitor_callback(frame: BusTrafficItem):
        nonlocal last_frame_counter, missed_frames, got_frames
        if last_frame_counter is None:
            last_frame_counter = frame.frame_counter
        elif frame.frame_counter is not None:
            delta = (frame.frame_counter - last_frame_counter - 1) % FRAME_COUNTER_MODULO
            missed_frames += delta
            last_frame_counter = frame.frame_counter
        if frame.frame_counter is not None:
            got_frames += 1

    async with client:
        dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
        driver_bus1 = WBDALIDriverNew(
            WBDALIDriverNewConfig(device_name=args.gateway, bus=1),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await driver_bus1.initialize()
        driver_bus2 = WBDALIDriverNew(
            WBDALIDriverNewConfig(device_name=args.gateway, bus=2),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await driver_bus2.initialize()

        cmds = [QueryActualLevel(GearShort(i)) for i in range(1, SEND_BATCH_SIZE)]

        driver_bus1.bus_traffic.register(bus_monitor_callback)

        try:
            cancel_event = asyncio.Event()

            def signal_handler():
                cancel_event.set()

            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

            current_task = asyncio.create_task(driver_bus2.send_commands(cmds))
            total_frames += len(cmds)
            next_task = None
            try:
                while not cancel_event.is_set():
                    if not cancel_event.is_set():
                        next_task = asyncio.create_task(driver_bus2.send_commands(cmds))
                        total_frames += len(cmds)
                    else:
                        next_task = None
                    await current_task
                    current_task = next_task
            finally:
                if next_task is not None and not next_task.done():
                    await next_task
        finally:
            await driver_bus2.deinitialize()
            await driver_bus1.deinitialize()
            dispatcher_task.cancel()
            await dispatcher_task
            print(
                f"Missed frames: {missed_frames}, received frames: {got_frames}, send frames: {total_frames}"
            )


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
