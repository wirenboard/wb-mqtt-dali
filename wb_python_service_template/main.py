import argparse
import json
import os
import queue
import signal
import sys
import threading

import jsonschema
from wb_common.mqtt_client import MQTTClient

# путь к пользовательским настройкам по умолчанию
CONFIG_FILEPATH = "/etc/wb-python-service-template.conf"
# указываем путь к статической схеме
SCHEMA_FILEPATH = "/usr/share/wb-mqtt-confed/schemas/wb-python-service-template.schema.json"


class OneThreadServiceTemplate:  # pylint:disable=too-few-public-methods
    def __init__(self):
        signal.signal(signal.SIGINT, self._signal_handler)

        self._client = MQTTClient("test_client", is_threaded=False)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._mqtt_was_disconected = False
        self._count = 0
        self._error = None

    def _on_connect(self, _client, _userdata, _flags, rc):
        if rc != 0:
            print("MQTT client connected with rc %s", rc)
            return

        print("MQTT client connected")
        print("Subscribe to topics")
        self._client.subscribe("/devices/power_status/controls/Vin")
        print("Publish RPC endpoints")
        # публикация rpc

        if self._mqtt_was_disconected:
            print("Republish controls")
            # перепубликация контролов

    def _on_disconnect(self, _client, _userdata, _flags):
        self._mqtt_was_disconected = True
        print("MQTT client disconnected")

    def _on_message(self, _client, _userdata, _msg):
        self._count += 1
        if self._count == 10:
            self._error = RuntimeError("Simulate failure")
            self._client.stop()

        print("Do work, publish result")
        # выполнение работы после получения сообщения и публикация результата

    def _signal_handler(self, _signum, _frame):
        print("Termination signal received, stopping MQTT client")
        self._client.stop()

    def run(self):
        try:
            print("Starting MQTT client")
            self._client.start()
            self._client.loop_forever()
        except ConnectionError:
            print("MQTT connection error!")
            return 1

        if self._error:
            print(f"Error occurred: {self._error}")
            return 1

        return 0


class ThreadedServiceTemplate:  # pylint:disable=too-few-public-methods
    def __init__(self):
        self._term_event = threading.Event()
        self._queue = queue.Queue()
        signal.signal(signal.SIGINT, self._signal_handler)

        self._client = MQTTClient("test_client")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._mqtt_was_disconected = False

    def _on_connect(self, _client, _userdata, _flags, rc):
        if rc != 0:
            print("MQTT client connected with rc %s", rc)
            return

        print("MQTT client connected")
        print("Subscribe to topics")
        self._client.subscribe("/devices/power_status/controls/Vin")
        print("Publish RPC endpoints")
        # публикация rpc

        if self._mqtt_was_disconected:
            print("Republish controls")
            # перепубликация контролов

    def _on_disconnect(self, _client, _userdata, _flags):
        self._mqtt_was_disconected = True
        print("MQTT client disconnected")

    def _on_message(self, _client, _userdata, msg):
        self._queue.put(msg.payload.decode("utf-8"))

    def _signal_handler(self, _signum, _frame):
        print("Termination signal received, stopping MQTT client")
        self._term_event.set()
        self._client.stop()

    def _do_work(self):
        count = 0
        while count != 10 and not self._term_event.is_set():
            if not self._queue.empty():
                count += 1
                message = self._queue.get()
                print("Handling new message " + message)
        if count == 10:
            raise RuntimeError("Simulate failure")

    def run(self):
        try:
            print("Starting MQTT client")
            self._client.start()
            self._do_work()
        except ConnectionError:
            print("MQTT connection error!")
            return 1
        except RuntimeError:
            print("Failure! Stopping MQTT client")
            self._client.stop()
            return 1

        return 0


def main(argv):
    parser = argparse.ArgumentParser(description="MQTT Python Service Template")
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
        return 6

    with open(args.config, "r", encoding="utf-8") as config_file, open(
        SCHEMA_FILEPATH, "r", encoding="utf-8"
    ) as schema_file:
        config = json.load(config_file)
        schema = json.load(schema_file)
        try:
            jsonschema.validate(
                instance=config, schema=schema, format_checker=jsonschema.Draft4Validator.FORMAT_CHECKER
            )
        except jsonschema.ValidationError as e:
            print(f"Configuration validation failed: {e.message}")
            return 6

    if config["mode"] == "one_thread":
        service = OneThreadServiceTemplate()
    else:
        service = ThreadedServiceTemplate()
    return service.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
