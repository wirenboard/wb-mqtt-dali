import argparse
import asyncio
import json
import os
import signal
import sys

import asyncio_mqtt
import jsonschema
from dali.address import DeviceBroadcast
from dali.device.general import StartQuiescentMode, StopQuiescentMode

from wb.mqtt_dali.commissioning import Commissioning
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_server import MQTTRPCServer
from wb.mqtt_dali.wbdali import (
    AsyncDeviceInstanceTypeMapper,
    WBDALIConfig,
    WBDALIDriver,
)

CONFIG_FILEPATH = "/etc/wb-mqtt-dali.conf"
SCHEMA_FILEPATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-dali.schema.json"

EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6


async def dali():
    # todo
    dev_inst_map = AsyncDeviceInstanceTypeMapper()
    cfg = WBDALIConfig(
        modbus_port_path="/dev/ttyRS485-1",
        device_name="wb-mdali_1",
        mqtt_host="localhost",
        mqtt_port=1883,
    )
    dev = WBDALIDriver(cfg, dev_inst_map=dev_inst_map)
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
    await cancel_event.wait()
    raise asyncio.CancelledError()


async def rpcHandler(params: dict) -> dict:
    return {"status": "ok"}


async def rpc(rpc_server: MQTTRPCServer):
    try:
        await rpc_server.add_endpoint(
            "Editor",
            "GetList",
            rpcHandler,
        )
        await rpc_server.start()
    except asyncio.CancelledError:
        pass


async def dispatcher(mqtt_dispatcher: MQTTDispatcher):
    try:
        await mqtt_dispatcher.run()
    except asyncio.CancelledError:
        pass


async def main(argv):
    parser = argparse.ArgumentParser(description="Wiren Board MQTT DALI Bridge")
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default=CONFIG_FILEPATH,
        help="Path to configuration file",
    )
    args = parser.parse_args(argv[1:])
    if not os.path.isfile(args.config):
        print(f"Configuration file not found: {args.config}")
        return EXIT_NOTCONFIGURED

    with open(args.config, "r", encoding="utf-8") as config_file, open(
        SCHEMA_FILEPATH, "r", encoding="utf-8"
    ) as schema_file:
        config = json.load(config_file)
        schema = json.load(schema_file)
        try:
            jsonschema.validate(
                instance=config, schema=schema, format_checker=jsonschema.draft4_format_checker
            )
        except jsonschema.ValidationError as e:
            print(f"Configuration validation failed: {e.message}")
            return EXIT_NOTCONFIGURED

    client = asyncio_mqtt.Client("10.0.0.225", 1883)

    mqtt_dispatcher = MQTTDispatcher(client)
    rpc_server = MQTTRPCServer("wb-mqtt-dali", mqtt_dispatcher)
    while True:
        try:
            async with client:
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

        except asyncio_mqtt.MqttError as e:
            print(f"Connection lost: {e}")
            rpc_server.clear()
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
