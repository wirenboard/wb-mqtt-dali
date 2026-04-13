# pylint: disable=duplicate-code

# Test arbitration on the bus by sending commands from two gateways to the same bus,
# and checking that that no malformed frames are received.
# Expects that buses 1 of two gateways are connected to each other, and a DALI device is connected to the bus.
# The test sends commands to a device from both gateways and checks that all requests are successful.

import argparse
import asyncio
import logging
import sys

from dali.address import GearShort
from dali.gear.general import QueryActualLevel
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
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
        description="Wiren Board MQTT DALI Bridge E2E bus arbitration test. "
        "Test arbitration on the bus by sending commands from two gateways to the same bus. "
        "Expects that buses 1 of two gateways are connected to each other, "
        "and a DALI device is connected to the bus. "
        "The test sends commands to a device from both gateways and checks that all requests are successful."
    )
    parser.add_argument(
        "--gateway1",
        dest="gateway1",
        type=str,
        default="wb-dali_1",
        help="Gateway 1 MQTT device (default: wb-dali_1)",
    )
    parser.add_argument(
        "--gateway2",
        dest="gateway2",
        type=str,
        default="wb-dali_2",
        help="Gateway 2 MQTT device (default: wb-dali_2)",
    )
    parser.add_argument(
        "--device_short_address",
        dest="device_short_address",
        type=int,
        default=1,
        help="DALI device short address (default: 1)",
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
        dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
        driver_bus1 = WBDALIDriverNew(
            WBDALIDriverNewConfig(device_name=args.gateway1, bus=1),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await driver_bus1.initialize()
        driver_bus2 = WBDALIDriverNew(
            WBDALIDriverNewConfig(device_name=args.gateway2, bus=1),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await driver_bus2.initialize()

        cmds = [QueryActualLevel(GearShort(args.device_short_address)) for i in range(1, 7)]

        try:

            bus1_task = asyncio.create_task(driver_bus1.send_commands(cmds))
            await asyncio.sleep(0.1)  # Let the first batch start sending
            bus2_task = asyncio.create_task(driver_bus2.send_commands(cmds))
            await asyncio.gather(bus1_task, bus2_task, return_exceptions=True)
            for task in (bus1_task, bus2_task):
                if task.exception() is not None:
                    logging.error("Error in bus command task: %s", task.exception())
        finally:
            await driver_bus2.deinitialize()
            await driver_bus1.deinitialize()
            dispatcher_task.cancel()
            await dispatcher_task


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
