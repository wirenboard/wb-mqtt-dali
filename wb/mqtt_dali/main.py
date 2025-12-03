import argparse
import asyncio
import json
import logging
import os
import sys
from urllib.parse import urlparse

import asyncio_mqtt as aiomqtt
import jsonschema
from dali.address import DeviceBroadcast
from dali.device.general import StartQuiescentMode, StopQuiescentMode

from wb.mqtt_dali.commissioning import Commissioning
from wb.mqtt_dali.wbdali import (
    AsyncDeviceInstanceTypeMapper,
    WBDALIConfig,
    WBDALIDriver,
)

CONFIG_FILEPATH = "/etc/wb-mqtt-dali.conf"
SCHEMA_FILEPATH = "/usr/share/wb-mqtt-confed/schemas/wb-mqtt-dali.schema.json"
DEFAULT_BROKER_URL = "unix:///var/run/mosquitto/mosquitto.sock"

EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6


def create_mqtt_client_factory(broker_url: str):
    url = urlparse(broker_url)

    if url.scheme == "unix":
        hostname = url.path
        port = 0
    else:
        hostname = url.hostname
        port = url.port

    auth = {}
    if url.username:
        auth["username"] = url.username
    if url.password:
        auth["password"] = url.password

    return lambda: aiomqtt.Client(
        hostname=hostname,
        port=port,
        transport="websockets" if url.scheme == "ws" else url.scheme,
        **auth,
    )


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

    with (
        open(args.config, "r", encoding="utf-8") as config_file,
        open(SCHEMA_FILEPATH, "r", encoding="utf-8") as schema_file,
    ):
        config = json.load(config_file)
        schema = json.load(schema_file)
        try:
            jsonschema.validate(
                instance=config,
                schema=schema,
                format_checker=jsonschema.draft4_format_checker,
            )
        except jsonschema.ValidationError as e:
            print(f"Configuration validation failed: {e.message}")
            return EXIT_NOTCONFIGURED

    if config["debug"]:
        logging.basicConfig(level=logging.DEBUG)

    mqtt_client_factory = create_mqtt_client_factory(DEFAULT_BROKER_URL)

    # todo
    dev_inst_map = AsyncDeviceInstanceTypeMapper()
    cfg = WBDALIConfig(
        modbus_port_path="/dev/ttyRS485-1",
        device_name="wb-mdali_1",
    )
    dev = WBDALIDriver(cfg, dev_inst_map=dev_inst_map, mqtt_client_factory=mqtt_client_factory)
    await dev.connect()
    await dev.connected.wait()
    await asyncio.sleep(1)
    await dev.send(StartQuiescentMode(DeviceBroadcast()))
    obj = Commissioning(dev, None, load=False)
    await obj.smart_extend()
    await dev.send(StopQuiescentMode(DeviceBroadcast()))
    dev.disconnect()

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
