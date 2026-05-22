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
        "Sends commands continuously on one bus and counts what arrives at "
        "another bus's bus_monitor. Two stand variants are supported: "
        "single-gateway (bus 1 and bus 2 of the same gateway wired to each "
        "other) and two-gateway (one bus of each gateway wired to each other). "
        "Defaults reproduce the original single-gateway behaviour."
    )
    parser.add_argument(
        "--gateway",
        dest="gateway",
        type=str,
        default="wb-dali_1",
        help=(
            "Gateway MQTT device for single-gateway mode (default: wb-dali_1). "
            "Used as the default for --send-gateway and --listen-gateway."
        ),
    )
    parser.add_argument(
        "--send-gateway",
        dest="send_gateway",
        type=str,
        default=None,
        help="Gateway that sends commands (default: --gateway).",
    )
    parser.add_argument(
        "--send-bus",
        dest="send_bus",
        type=int,
        default=2,
        help="Bus number on the sending gateway (default: 2).",
    )
    parser.add_argument(
        "--listen-gateway",
        dest="listen_gateway",
        type=str,
        default=None,
        help="Gateway whose bus_monitor we count (default: --gateway).",
    )
    parser.add_argument(
        "--listen-bus",
        dest="listen_bus",
        type=int,
        default=1,
        help="Bus number on the listening gateway (default: 1).",
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
    send_gateway = args.send_gateway or args.gateway
    listen_gateway = args.listen_gateway or args.gateway
    if send_gateway == listen_gateway and args.send_bus == args.listen_bus:
        parser.error("send and listen targets must differ (same gateway+bus is meaningless)")

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
        logging.info(
            "Sender: %s bus %d. Listener (bus_monitor counted): %s bus %d.",
            send_gateway,
            args.send_bus,
            listen_gateway,
            args.listen_bus,
        )
        listener = WBDALIDriverNew(
            WBDALIDriverNewConfig(device_name=listen_gateway, bus=args.listen_bus),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await listener.initialize()
        sender = WBDALIDriverNew(
            WBDALIDriverNewConfig(device_name=send_gateway, bus=args.send_bus),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await sender.initialize()

        cmds = [QueryActualLevel(GearShort(i)) for i in range(1, SEND_BATCH_SIZE)]

        listener.bus_traffic.register(bus_monitor_callback)

        try:
            cancel_event = asyncio.Event()

            def signal_handler():
                cancel_event.set()

            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

            current_task = asyncio.create_task(sender.send_commands(cmds))
            total_frames += len(cmds)
            next_task = None
            try:
                while not cancel_event.is_set():
                    if not cancel_event.is_set():
                        next_task = asyncio.create_task(sender.send_commands(cmds))
                        total_frames += len(cmds)
                    else:
                        next_task = None
                    await current_task
                    current_task = next_task
            finally:
                if next_task is not None and not next_task.done():
                    await next_task
        finally:
            await sender.deinitialize()
            await listener.deinitialize()
            dispatcher_task.cancel()
            await dispatcher_task
            print(
                f"Missed frames: {missed_frames}, received frames: {got_frames}, send frames: {total_frames}"
            )


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
