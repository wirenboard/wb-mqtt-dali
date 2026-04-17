# pylint: disable=duplicate-code

import argparse
import asyncio
import logging
import signal
import sys

from dali.address import GearShort
from dali.gear.general import QueryActualLevel
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.mqtt_dali.bus_traffic import BusTrafficItem
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import FRAME_COUNTER_MODULO
from wb.mqtt_dali.wbdali import WBDALIConfig as WBDALIDriverNewConfig
from wb.mqtt_dali.wbdali import WBDALIDriver as WBDALIDriverNew
from wb.mqtt_dali.wbmqtt import make_mqtt_client

EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6
SEND_BATCH_SIZE = 16


async def dispatcher(mqtt_dispatcher: MQTTDispatcher):
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        # Allow graceful shutdown on cancellation; no cleanup needed here.
        pass


async def main(argv):  # pylint: disable=too-many-locals,too-many-statements
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
        default="wb-mdali_1",
        help="Gateway MQTT device (default: wb-mdali_1)",
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
