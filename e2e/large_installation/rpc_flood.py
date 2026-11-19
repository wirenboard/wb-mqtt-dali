# RPC flood of S1: once wb-mqtt-serial answers `config/Load`, a burst of fw-info requests to the
# gateway queues ahead of wb-mqtt-dali's `port/Load` writes for longer than their total_timeout.

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional

from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher

SERIAL_RPC_PREFIX = "/rpc/v1/wb-mqtt-serial"
PORT_KEYS = ("path", "baud_rate", "parity", "stop_bits", "data_bits")
FW_INFO_METHOD = "fw-update/GetFirmwareInfo"
CONFIG_LOAD_METHOD = "config/Load"
FLOOD_REQUESTS = 200
CONFIG_LOAD_TIMEOUT_S = 1.0
CONFIG_LOAD_POLL_S = 0.2


def request_params(port: dict, slave_id: int) -> dict:
    """Params of one flood request about `slave_id`."""
    line = {key: port[key] for key in PORT_KEYS if key in port}
    return {"slave_id": slave_id, "port": line, "protocol": "modbus"}


def error_data(reply: dict) -> Optional[str]:
    """The `data` (or `message`) of a JSON-RPC error reply, None for a result."""
    error = reply.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        return str(error.get("data") or error.get("message") or error)
    return str(error)


class RpcFlood:
    def __init__(self, dispatcher: MQTTDispatcher, port: dict, slave_id: int) -> None:
        self._dispatcher = dispatcher
        self._port = port
        self._slave_id = slave_id
        self._client_id = f"e2e-rpc-flood-{uuid.uuid4().hex[:8]}"
        self._next_id = 0
        self._replies: dict[int, asyncio.Future] = {}
        self._main: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Send the burst once wb-mqtt-serial answers `config/Load`."""
        self._main = asyncio.create_task(self._run())

    async def stop(self) -> None:
        await self._dispatcher.unsubscribe(self._reply_topic(), self.handle_reply)
        if self._main is not None:
            self._main.cancel()
            await asyncio.gather(self._main, return_exceptions=True)
            self._main = None
        self._replies.clear()

    def handle_reply(self, message) -> None:
        try:
            body = json.loads(message.payload)
        except ValueError:
            return
        future = self._replies.pop(body.get("id"), None) if isinstance(body, dict) else None
        if future is not None and not future.done():
            future.set_result(body)

    # --- Private ---

    def _reply_topic(self) -> str:
        return f"{SERIAL_RPC_PREFIX}/+/+/{self._client_id}/reply"

    async def _run(self) -> None:
        await self._dispatcher.subscribe(self._reply_topic(), self.handle_reply)
        while error_data(await self._config_load()) is not None:
            await asyncio.sleep(CONFIG_LOAD_POLL_S)
        params = request_params(self._port, self._slave_id)
        for _ in range(FLOOD_REQUESTS):
            await self._publish(FW_INFO_METHOD, params)

    async def _config_load(self) -> dict:
        """The `config/Load` reply body; no reply in time comes back as an error."""
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._replies[request_id] = future
        await self._publish(CONFIG_LOAD_METHOD, {}, request_id)
        try:
            return await asyncio.wait_for(future, CONFIG_LOAD_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._replies.pop(request_id, None)
            return {"error": {"data": "reply timeout"}}

    async def _publish(self, method: str, params: dict, request_id: Optional[int] = None) -> None:
        if request_id is None:
            self._next_id += 1
            request_id = self._next_id
        await self._dispatcher.publish(
            f"{SERIAL_RPC_PREFIX}/{method}/{self._client_id}",
            json.dumps({"id": request_id, "params": params}),
            qos=1,
        )
