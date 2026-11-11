"""wb-mqtt-serial's answers to the driver's `port/Load` writes, for driver tests."""

import asyncio
import json
import unittest
from typing import Optional

import aiomqtt

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

from ._fake_rpc_client import FakePortLoadClient

QUEUE_RESET_FUNCTION = 6

RPC_REQUEST_TIMEOUT_ERROR = {"code": -32600, "message": "Request timeout", "data": "RPC request timeout"}
SERIAL_PROTOCOL_ERROR = {
    "code": -32600,
    "message": "Request timeout",
    "data": "Serial protocol error: request timed out",
}
DEVICE_SETUP_FAILED_ERROR = {"code": -32000, "message": "Server error", "data": "Device setup failed"}


def is_queue_reset(payload: str) -> bool:
    return json.loads(payload)["params"]["function"] == QUEUE_RESET_FUNCTION


def port_load_reply(
    request_topic: str, request_payload: str, error: Optional[dict] = None
) -> aiomqtt.Message:
    """The RPC reply to a `port/Load` request: its acknowledgement, or `error`."""
    body: dict = {"id": json.loads(request_payload)["id"]}
    if error is None:
        body["result"] = {"response": ""}
    else:
        body["error"] = error
    return aiomqtt.Message(f"{request_topic}/reply", json.dumps(body), 0, False, 0, None)


async def wait_for_batch(client, topic: str) -> str:
    """The payload of the client's next queue write to `topic`, skipping pointer resets."""
    while True:
        payload = await client.wait_for_publish(topic)
        if not is_queue_reset(payload):
            return payload


async def wait_for_batch_write(rpc: FakePortLoadClient) -> dict:
    """The params of the next queue write taken by `rpc`, skipping pointer resets."""
    while True:
        params = await rpc.wait_for_write()
        if params["function"] != QUEUE_RESET_FUNCTION:
            return params


def run_dispatcher(test: unittest.IsolatedAsyncioTestCase, dispatcher: MQTTDispatcher) -> None:
    """Run the dispatcher's message loop for the rest of the test."""
    task = asyncio.create_task(dispatcher.run())

    async def stop() -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    test.addAsyncCleanup(stop)
