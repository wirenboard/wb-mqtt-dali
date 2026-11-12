# What S1 and S2 add to a cold start report: wb-mqtt-dali queue writes that wb-mqtt-serial failed,
# seen on the port/Load RPC topics.

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

from e2e.large_installation.rpc_flood import error_data
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig

DRIVER = WBDALIConfig()
PORT_LOAD_REQUESTS = "/rpc/v1/wb-mqtt-serial/port/Load/+"
PORT_LOAD_REPLIES = PORT_LOAD_REQUESTS + "/reply"
RPC_CLIENT_ID_LEVEL = 6
WRITE_FUNCTIONS = (6, 16)


@dataclass(frozen=True)
class QueueTarget:
    """Slots or the pointer of one bus queue of the gateway, as a Modbus write hits them."""

    bus: int
    slots: tuple[int, ...]
    pointer: bool

    def __str__(self) -> str:
        if self.pointer:
            return f"bus {self.bus} pointer"
        if len(self.slots) == 1:
            return f"bus {self.bus} slot {self.slots[0]}"
        return f"bus {self.bus} slots {self.slots[0]}-{self.slots[-1]}"


def queue_target(address: int, count: int) -> Optional[QueueTarget]:
    """None for an address outside every bus queue and pointer."""
    offset = address - DRIVER.queue_start_modbus_address
    if offset < 0:
        return None
    bus_index, register = divmod(offset, DRIVER.queue_modbus_bus_offset)
    pointer_register = DRIVER.queue_bulk_send_pointer_modbus_address - DRIVER.queue_start_modbus_address
    if register == pointer_register:
        return QueueTarget(bus_index + 1, (), True)
    if register >= 2 * DRIVER.queue_size:
        return None
    last = min(register + max(count, 1) - 1, 2 * DRIVER.queue_size - 1)
    return QueueTarget(bus_index + 1, tuple(range(register // 2, last // 2 + 1)), False)


@dataclass(frozen=True)
class FailedWrite:
    sent_at: float
    target: QueueTarget
    wait_s: float
    error: str


@dataclass(frozen=True)
class _PendingWrite:
    sent_at: float
    target: QueueTarget


class PortLoadSniffer:
    """wb-mqtt-dali writes to the queues of gateway `device_id` that got an RPC error back."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.failed: list[FailedWrite] = []
        self._pending: dict[tuple[str, object], _PendingWrite] = {}

    async def start(self, dispatcher: MQTTDispatcher) -> None:
        self.failed = []
        self._pending = {}
        await dispatcher.subscribe(PORT_LOAD_REQUESTS, self.handle_request)
        await dispatcher.subscribe(PORT_LOAD_REPLIES, self.handle_reply)

    async def stop(self, dispatcher: MQTTDispatcher) -> None:
        await dispatcher.unsubscribe(PORT_LOAD_REQUESTS, self.handle_request)
        await dispatcher.unsubscribe(PORT_LOAD_REPLIES, self.handle_reply)

    def handle_request(self, message) -> None:
        body = _json_body(message)
        if body is None:
            return
        params = body.get("params") or {}
        if params.get("device_id") != self.device_id or params.get("function") not in WRITE_FUNCTIONS:
            return
        target = queue_target(int(params.get("address", -1)), int(params.get("count", 1)))
        if target is not None:
            self._pending[(_client_id(message), body.get("id"))] = _PendingWrite(time.time(), target)

    def handle_reply(self, message) -> None:
        body = _json_body(message)
        if body is None:
            return
        pending = self._pending.pop((_client_id(message), body.get("id")), None)
        error = error_data(body)
        if pending is None or error is None:
            return
        self.failed.append(FailedWrite(pending.sent_at, pending.target, time.time() - pending.sent_at, error))


def _json_body(message) -> Optional[dict]:
    if message.retain:
        return None
    try:
        body = json.loads(message.payload)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _client_id(message) -> str:
    return str(message.topic).split("/")[RPC_CLIENT_ID_LEVEL]
