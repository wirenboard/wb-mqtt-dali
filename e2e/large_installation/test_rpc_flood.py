import asyncio
import json
import unittest
import unittest.mock
from typing import Callable, Optional

from e2e.large_installation import rpc_flood
from e2e.large_installation.rpc_flood import RpcFlood, error_data, request_params
from e2e.large_installation.test_stand import Message

PORT = {
    "path": "/dev/ttyX",
    "baud_rate": 115200,
    "parity": "N",
    "stop_bits": 2,
    "data_bits": 8,
    "devices": [
        {"device_type": "WB-MR6C v.3", "slave_id": "32"},
        {"device_type": "WB-LED-since-fw-3.8", "slave_id": "182", "id": "led"},
        {"device_type": "WB-MSW v.4", "slave_id": "11", "enabled": False},
        {"device_type": "ONOKOM-AIR-DK-5-MB-B", "slave_id": "1"},
    ],
}


class FakeDispatcher:
    """Records publications; `answer` makes up the reply to each RPC request, None for no reply."""

    def __init__(self, answer: Callable[[str, dict], Optional[dict]]) -> None:
        self.answer = answer
        self.requests: list[tuple[str, dict]] = []
        self.subscriptions: dict[str, Callable] = {}

    async def subscribe(self, topic: str, callback: Callable) -> None:
        self.subscriptions[topic] = callback

    async def unsubscribe(self, topic: str, callback: Optional[Callable] = None) -> None:
        if self.subscriptions.get(topic) == callback:
            del self.subscriptions[topic]

    # pylint: disable-next=unused-argument
    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        body = json.loads(payload)
        method = "/".join(topic.split("/")[4:6])
        self.requests.append((method, body["params"]))
        reply = self.answer(method, body["params"])
        if reply is not None:
            reply_topic = topic + "/reply"
            callback = next(c for t, c in self.subscriptions.items() if t.endswith("/reply"))
            message = Message(reply_topic, json.dumps({"id": body["id"], **reply}).encode())
            asyncio.get_running_loop().call_soon(callback, message)

    def sent(self, method: str) -> list[dict]:
        return [params for m, params in self.requests if m == method]


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


class RequestParamsTest(unittest.TestCase):
    def test_request_params(self):
        params = request_params(PORT, 32)
        self.assertEqual(params["port"], {k: PORT[k] for k in rpc_flood.PORT_KEYS})
        self.assertEqual((params["slave_id"], params["protocol"]), (32, "modbus"))

    def test_error_data(self):
        self.assertIsNone(error_data({"result": {}}))
        self.assertEqual(error_data({"error": {"code": -1, "data": "config-busy"}}), "config-busy")
        self.assertEqual(error_data({"error": {"message": "boom"}}), "boom")


class RpcFloodTest(unittest.IsolatedAsyncioTestCase):
    async def test_burst_follows_the_config_load_answer(self):
        """config/Load fails once, then answers; then the whole burst about the gateway goes out."""
        config_loads = []

        def answer(method, _params):
            if method == "config/Load":
                config_loads.append(1)
                return {"error": {"data": "not yet"}} if len(config_loads) == 1 else {"result": {}}
            return None

        dispatcher = FakeDispatcher(answer)
        flood = RpcFlood(dispatcher, PORT, 234)
        with unittest.mock.patch.object(rpc_flood, "CONFIG_LOAD_POLL_S", 0):
            flood.start()
            self.addAsyncCleanup(flood.stop)
            await settle()
        self.assertEqual(len(config_loads), 2)
        self.assertEqual(
            [p["slave_id"] for p in dispatcher.sent("fw-update/GetFirmwareInfo")],
            [234] * rpc_flood.FLOOD_REQUESTS,
        )

    async def test_stop_unsubscribes_everything(self):
        dispatcher = FakeDispatcher(lambda method, params: {"result": {}})
        flood = RpcFlood(dispatcher, PORT, 234)
        flood.start()
        await settle()
        await flood.stop()
        self.assertEqual(dispatcher.subscriptions, {})
