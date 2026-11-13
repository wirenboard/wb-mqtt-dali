import json
import unittest

from e2e.large_installation.startup_traffic import (
    PortLoadSniffer,
    QueueTarget,
    queue_target,
)
from e2e.large_installation.test_stand import Message

REQUESTS = "/rpc/v1/wb-mqtt-serial/port/Load/wb-mqtt-dali-abc-bus1"


def request(request_id: int, address: int, count: int = 2, function: int = 16, device: str = "wb-dali_1"):
    params = {"device_id": device, "function": function, "address": address, "count": count}
    return Message(REQUESTS, json.dumps({"id": request_id, "params": params}).encode())


def reply(request_id: int, error=None):
    body = {"id": request_id, **({"error": error} if error else {"result": {}})}
    return Message(REQUESTS + "/reply", json.dumps(body).encode())


class QueueTargetTest(unittest.TestCase):
    def test_slots_and_pointer_of_each_bus(self):
        self.assertEqual(queue_target(1400, 2), QueueTarget(1, (0,), False))
        self.assertEqual(queue_target(2404, 4), QueueTarget(2, (2, 3), False))
        self.assertEqual(queue_target(3430, 2), QueueTarget(3, (15,), False))
        self.assertEqual(queue_target(1432, 1), QueueTarget(1, (), True))
        self.assertIsNone(queue_target(1500, 1))
        self.assertIsNone(queue_target(250, 16))

    def test_description(self):
        self.assertEqual(str(QueueTarget(2, (2, 3), False)), "bus 2 slots 2-3")
        self.assertEqual(str(QueueTarget(1, (5,), False)), "bus 1 slot 5")
        self.assertEqual(str(QueueTarget(3, (), True)), "bus 3 pointer")


class PortLoadSnifferTest(unittest.TestCase):
    def test_only_failed_queue_writes_of_the_gateway_are_kept(self):
        """A failed slot write is kept with its error.
        A success, a read and another device's write are not."""
        sniffer = PortLoadSniffer("wb-dali_1")
        sniffer.handle_request(request(1, 2410))
        sniffer.handle_request(request(2, 1400))
        sniffer.handle_request(request(3, 1500, 16, function=3))
        sniffer.handle_request(request(4, 1400, device="wb-dali_2"))
        sniffer.handle_reply(reply(2))
        for request_id in (3, 4, 1):
            sniffer.handle_reply(reply(request_id, {"code": -32100, "data": "RPC request timeout"}))
        self.assertEqual(len(sniffer.failed), 1)
        failed = sniffer.failed[0]
        self.assertEqual((failed.target, failed.error), (QueueTarget(2, (5,), False), "RPC request timeout"))
        self.assertGreaterEqual(failed.wait_s, 0.0)

    def test_retained_and_unrelated_replies_are_ignored(self):
        sniffer = PortLoadSniffer("wb-dali_1")
        sniffer.handle_request(request(1, 1432, 1, function=6))
        sniffer.handle_reply(Message(REQUESTS + "/reply", b'{"id": 1, "error": {"data": "x"}}', retain=True))
        sniffer.handle_reply(
            Message("/rpc/v1/wb-mqtt-serial/port/Load/other/reply", b'{"id": 1, "error": "x"}')
        )
        self.assertEqual(sniffer.failed, [])
        sniffer.handle_reply(reply(1, "plain error"))
        self.assertEqual(
            [(str(f.target), f.error) for f in sniffer.failed], [("bus 1 pointer", "plain error")]
        )
