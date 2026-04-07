import argparse
import asyncio
import json
import logging
import os
import signal
import sys

import aiomqtt
import jsonschema
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from .commissioning import Commissioning, check_presence, search_short
from .config_validator import validate_config
from .gateway import Gateway
from .gtin_db import DaliDatabase
from .mqtt_dispatcher import MQTTDispatcher
from .send_command import (
    build_command_registry,
    format_response,
    list_commands,
    parse_and_build_command,
)
from .wbdali import WBDALIConfig as WBDALIDriverNewConfig
from .wbdali import WBDALIDriver as WBDALIDriverNew
from .wbdali_utils import send_commands_with_retry, send_with_retry
from .wbmqtt import make_mqtt_client

CONFIG_FILEPATH = "/etc/wb-mqtt-dali.conf"
WB_SCHEMA_FILEPATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-dali.schema.json"
DEV_SCHEMA_FILEPATH = "./wb-mqtt-dali.schema.json"
GTIN_DB_FILEPATH = "/usr/share/wb-mqtt-dali/products.csv"


EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6
SEND_BATCH_SIZE = 16


async def wait_for_cancel():
    cancel_event = asyncio.Event()

    def signal_handler():
        cancel_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)
    await cancel_event.wait()
    raise asyncio.CancelledError()


async def dispatcher(mqtt_dispatcher: MQTTDispatcher):
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        # Allow graceful shutdown on cancellation; no cleanup needed here.
        pass


def load_config(config_filepath: str) -> dict:
    schema_filepaths = [WB_SCHEMA_FILEPATH, DEV_SCHEMA_FILEPATH]
    schema = None
    for schema_filepath in schema_filepaths:
        if os.path.isfile(schema_filepath):
            with open(schema_filepath, "r", encoding="utf-8") as schema_file:
                schema = json.load(schema_file)
            break
    if schema is None:
        raise FileNotFoundError("Schema file not found")

    with open(config_filepath, "r", encoding="utf-8") as config_file:
        config = json.load(config_file)
        jsonschema.validate(instance=config, schema=schema, format_checker=jsonschema.draft4_format_checker)
    validate_config(config)
    return config


async def default_service(args):
    try:
        config = load_config(args.config)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Failed to load configuration: %s", e)
        return EXIT_NOTCONFIGURED

    if config.get("debug"):
        logging.basicConfig(level=logging.DEBUG, force=True)
        logging.getLogger("mqtt_client").setLevel(logging.INFO)

    gtin_db = DaliDatabase(GTIN_DB_FILEPATH)

    client = make_mqtt_client(args.broker_url)

    mqtt_dispatcher = MQTTDispatcher(client)
    gateway = Gateway(config, mqtt_dispatcher, args.config, gtin_db)
    is_first_connection = True
    while True:
        try:
            async with client:
                is_first_connection = True
                dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
                gateway_task = asyncio.create_task(gateway.start())
                task_group = asyncio.gather(
                    dispatcher_task,
                    gateway_task,
                    wait_for_cancel(),
                )
                try:
                    await task_group
                except asyncio.CancelledError:
                    await gateway.stop()
                    dispatcher_task.cancel()
                    await dispatcher_task
                    break

        except aiomqtt.MqttError as e:
            await gateway.stop()
            if is_first_connection:
                is_first_connection = False
                logging.error("%s. Reconnecting", str(e))
            await asyncio.sleep(1)

    return EXIT_SUCCESS


async def check_presence_service(gateway: str, args, dali2: bool, bus: int = 1):
    client = make_mqtt_client(args.broker_url)

    mqtt_dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
        driver = WBDALIDriverNew(
            WBDALIDriverNewConfig(gateway, bus), mqtt_dispatcher=mqtt_dispatcher, logger=logging.getLogger()
        )
        await driver.initialize()
        if dali2:
            if await check_presence(driver, True):
                logging.info("DALI 2 devices are present")
            else:
                logging.info("DALI 2 devices are NOT present")
        else:
            if await check_presence(driver, False):
                logging.info("DALI devices are present")
            else:
                logging.info("DALI devices are NOT present")
        await driver.deinitialize()
        dispatcher_task.cancel()
        await dispatcher_task

    return EXIT_SUCCESS


async def binary_search_service(gateway: str, args, dali2: bool, bus: int = 1):
    client = make_mqtt_client(args.broker_url)
    mqtt_dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
        driver = WBDALIDriverNew(
            WBDALIDriverNewConfig(gateway, bus),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await driver.initialize()
        commissioning = Commissioning(driver, [], dali2)
        await commissioning.binary_search()
        await driver.deinitialize()
        dispatcher_task.cancel()
        await dispatcher_task

    return EXIT_SUCCESS


async def short_search_service(gateway: str, args, dali2: bool, bus: int = 1):
    client = make_mqtt_client(args.broker_url)
    mqtt_dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
        driver = WBDALIDriverNew(
            WBDALIDriverNewConfig(gateway, bus),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await driver.initialize()
        await search_short(driver, dali2)
        await driver.deinitialize()
        dispatcher_task.cancel()
        await dispatcher_task

    return EXIT_SUCCESS


async def send_command_service(
    gateway: str, args
):  # pylint: disable=too-many-locals, too-many-branches, too-many-statements
    registry = build_command_registry()

    data = int(args.data, 0) if args.data is not None else None
    cmd = parse_and_build_command(
        args.command,
        registry,
        address=args.address,
        data=data,
        group=args.group,
        broadcast=args.broadcast,
    )

    repeat = args.repeat
    client = make_mqtt_client(args.broker_url)
    mqtt_dispatcher = MQTTDispatcher(client)
    async with client:
        dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
        driver = WBDALIDriverNew(
            WBDALIDriverNewConfig(gateway, args.bus),
            mqtt_dispatcher=mqtt_dispatcher,
            logger=logging.getLogger(),
        )
        await driver.initialize()
        try:
            cancel_event = asyncio.Event()

            def signal_handler():
                cancel_event.set()

            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

            if repeat == 1:
                response = await send_with_retry(driver, cmd, logging.getLogger())
                print(format_response(response))
            else:
                scheduled = 0
                printed = 0
                bs = min(SEND_BATCH_SIZE, repeat) if repeat > 0 else SEND_BATCH_SIZE
                scheduled += bs
                current_task = asyncio.create_task(
                    send_commands_with_retry(driver, [cmd] * bs, logging.getLogger())
                )
                next_task = None
                try:
                    while not cancel_event.is_set():
                        bs = min(SEND_BATCH_SIZE, repeat - scheduled) if repeat > 0 else SEND_BATCH_SIZE
                        if bs > 0 and not cancel_event.is_set():
                            scheduled += bs
                            next_task = asyncio.create_task(
                                send_commands_with_retry(driver, [cmd] * bs, logging.getLogger())
                            )
                        else:
                            next_task = None
                        responses = await current_task
                        for response in responses:
                            printed += 1
                            print(f"[{printed}] {format_response(response)}")
                        if next_task is None:
                            break
                        current_task = next_task
                finally:
                    if next_task is not None and not next_task.done():
                        next_task.cancel()
                        try:
                            await next_task
                        except asyncio.CancelledError:
                            pass
        finally:
            await driver.deinitialize()
            dispatcher_task.cancel()
            await dispatcher_task

    return EXIT_SUCCESS


async def main(argv):  # pylint: disable=too-many-return-statements
    parser = argparse.ArgumentParser(description="Wiren Board MQTT DALI Bridge")
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default=CONFIG_FILEPATH,
        help="Path to configuration file",
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
    parser.add_argument(
        "-b",
        "--broker",
        "--broker_url",
        dest="broker_url",
        type=str,
        help="MQTT broker url",
        default=DEFAULT_BROKER_URL,
    )

    parser.add_argument(
        "--check-presence",
        dest="check_presence_gateway",
        type=str,
        help="Enable DALI device presence checking on specified gateway",
    )

    parser.add_argument(
        "--check-presence2",
        dest="check_presence2_gateway",
        type=str,
        help="Enable DALI 2 device presence checking on specified gateway",
    )

    parser.add_argument(
        "--binary-search",
        dest="binary_search_gateway",
        type=str,
        help="Binary search of DALI devices on specified gateway",
    )

    parser.add_argument(
        "--binary-search2",
        dest="binary_search2_gateway",
        type=str,
        help="Binary search of DALI 2 devices on specified gateway",
    )

    parser.add_argument(
        "--search-short",
        dest="search_short_gateway",
        type=str,
        help="Short address search of DALI devices on specified gateway",
    )

    parser.add_argument(
        "--search-short2",
        dest="search_short2_gateway",
        type=str,
        help="Short address search of DALI 2 devices on specified gateway",
    )

    parser.add_argument(
        "--bus",
        dest="bus",
        type=int,
        default=1,
        help="Bus number to use (default: 1)",
    )

    parser.add_argument(
        "--send-command",
        dest="send_command_gateway",
        type=str,
        help="Send a DALI command via specified gateway",
    )

    parser.add_argument(
        "--address",
        dest="address",
        type=int,
        help="DALI short address (0-63) for --send-command",
    )

    parser.add_argument(
        "--command",
        dest="command",
        type=str,
        help="DALI command name (e.g., Off, DAPC, DT8.Activate, FF24.QueryDeviceStatus)",
    )

    parser.add_argument(
        "--data",
        dest="data",
        type=str,
        help="Data value for commands that require it (e.g., DAPC power level, DTR value)",
    )

    parser.add_argument(
        "--group",
        dest="group",
        type=int,
        help="DALI group (0-15) or DALI 2 group (0-31) for --send-command",
    )

    parser.add_argument(
        "--broadcast",
        dest="broadcast",
        action="store_true",
        default=False,
        help="Send command as broadcast (no address needed)",
    )

    parser.add_argument(
        "--repeat",
        dest="repeat",
        type=int,
        default=1,
        help="Number of times to send the command (0 = infinite, until Ctrl+C)",
    )

    parser.add_argument(
        "--list-commands",
        dest="list_commands",
        action="store_true",
        default=False,
        help="List all available DALI commands",
    )

    args = parser.parse_args(argv[1:])

    logging.basicConfig(level=args.log_level)
    logging.getLogger("mqtt_client").setLevel(logging.INFO)

    if args.check_presence_gateway:
        return await check_presence_service(args.check_presence_gateway, args, dali2=False, bus=args.bus)
    if args.check_presence2_gateway:
        return await check_presence_service(args.check_presence2_gateway, args, dali2=True, bus=args.bus)
    if args.binary_search_gateway:
        return await binary_search_service(args.binary_search_gateway, args, dali2=False, bus=args.bus)
    if args.binary_search2_gateway:
        return await binary_search_service(args.binary_search2_gateway, args, dali2=True, bus=args.bus)
    if args.search_short_gateway:
        return await short_search_service(args.search_short_gateway, args, dali2=False, bus=args.bus)
    if args.search_short2_gateway:
        return await short_search_service(args.search_short2_gateway, args, dali2=True, bus=args.bus)
    if args.list_commands:
        registry = build_command_registry()
        print(list_commands(registry))
        return EXIT_SUCCESS
    if args.send_command_gateway:
        if not args.command:
            parser.error("--send-command requires --command")
        if args.repeat < 0:
            parser.error("--repeat must be non-negative (0 = infinite)")
        try:
            return await send_command_service(args.send_command_gateway, args)
        except ValueError as exc:
            parser.error(str(exc))
    return await default_service(args)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
