import argparse
import asyncio
import json
import os
import sys

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

EXIT_SUCCESS = 0
EXIT_NOTCONFIGURED = 6


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

    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv)))
