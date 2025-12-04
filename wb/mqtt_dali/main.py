import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from urllib.parse import urlparse

import asyncio_mqtt as aiomqtt
import jsonschema
from dali.address import DeviceBroadcast
from dali.device.general import StartQuiescentMode, StopQuiescentMode
from wb_common.mqtt_client import DEFAULT_BROKER_URL

from .commissioning import Commissioning
from .mqtt_dispatcher import MQTTDispatcher
from .mqtt_rpc_server import MQTTRPCServer
from .mqtt_rpc_server import logger as rpc_logger
from .wbdali import AsyncDeviceInstanceTypeMapper, WBDALIConfig, WBDALIDriver

CONFIG_FILEPATH = "/etc/wb-mqtt-dali.conf"
WB_SCHEMA_FILEPATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-dali.schema.json"
DEV_SCHEMA_FILEPATH = "./wb-mqtt-dali.schema.json"


EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6


async def dali(mqtt_dispatcher: MQTTDispatcher):
    # todo
    dev_inst_map = AsyncDeviceInstanceTypeMapper()
    cfg = WBDALIConfig(
        modbus_port_path="/dev/ttyRS485-1",
        device_name="wb-mdali_1",
    )
    dev = WBDALIDriver(cfg, dev_inst_map=dev_inst_map, mqtt_dispatcher=mqtt_dispatcher)
    await dev.connect()
    await dev.connected.wait()
    await asyncio.sleep(1)
    await dev.send(StartQuiescentMode(DeviceBroadcast()))
    obj = Commissioning(dev, None, load=False)
    await obj.smart_extend()
    await dev.send(StopQuiescentMode(DeviceBroadcast()))
    dev.disconnect()


async def wait_for_cancel():
    cancel_event = asyncio.Event()

    def signal_handler():
        cancel_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)
    await cancel_event.wait()
    raise asyncio.CancelledError()


async def rpc_handler(params: dict):
    return [
        {
            "id": "1",
            "name": "WB-MDALI3",
            "buses": [
                {
                    "id": "11",
                    "name": "Bus 1",
                    "groups": [
                        {"id": "group1", "name": 1},
                        {"id": "group3", "name": 3},
                        {"id": "group10", "name": 10},
                    ],
                    "devices": [
                        {"id": "111", "name": "MART-DIN#22", "groups": ["group1", "group3"]},
                        {"id": "222", "name": "Led-BR", "groups": []},
                        {"id": "333", "name": "SMART_LAMP 12", "groups": ["group3", "group10"]},
                    ],
                },
                {
                    "id": "22",
                    "name": "Bus 2",
                    "groups": [],
                    "devices": [
                        {"id": "444", "name": "Crystal Lamp", "groups": []},
                    ],
                },
            ],
        }
    ]


async def rpc(rpc_server: MQTTRPCServer):
    try:
        await rpc_server.add_endpoint(
            "Editor",
            "GetList",
            rpc_handler,
        )
        await rpc_server.start()
    except asyncio.CancelledError:
        # Allow graceful shutdown on cancellation; no cleanup needed here.
        pass


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
    client = aiomqtt.Client(
        hostname=hostname,
        port=port,
        transport="websockets" if urlparse_result.scheme == "ws" else urlparse_result.scheme,
        **auth,
    )
    return client


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
    return config


async def main(argv):
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
    args = parser.parse_args(argv[1:])

    try:
        config = load_config(args.config)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Failed to load configuration: %s", e)
        return EXIT_NOTCONFIGURED

    log_level = logging.DEBUG if config.get("debug") else args.log_level
    logging.basicConfig(level=log_level)
    rpc_logger.setLevel(log_level)

    client = make_mqtt_client(args.broker_url)

    mqtt_dispatcher = MQTTDispatcher(client)
    rpc_server = MQTTRPCServer("wb-mqtt-dali", mqtt_dispatcher)
    is_first_connection = True
    while True:
        try:
            async with client:
                is_first_connection = True
                dispatcher_task = asyncio.create_task(dispatcher(mqtt_dispatcher))
                rpc_task = asyncio.create_task(rpc(rpc_server))
                task_group = asyncio.gather(
                    dispatcher_task,
                    rpc_task,
                    wait_for_cancel(),
                )
                try:
                    await task_group
                except asyncio.CancelledError:
                    rpc_task.cancel()
                    await rpc_task
                    await rpc_server.stop()
                    dispatcher_task.cancel()
                    await dispatcher_task
                    break

        except aiomqtt.MqttError as e:
            if is_first_connection:
                is_first_connection = False
                logging.error("%s. Reconnecting", str(e))
            rpc_server.clear()
            await asyncio.sleep(1)

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
