import asyncio
from typing import Optional

from wb.mqtt_dali.mqtt_dispatcher import BrokerDisconnectedError, MQTTDispatcher
from wb.mqtt_dali.mqtt_rpc_server import RpcResult
from wb.mqtt_dali.wbdali import MODBUS_READ_HOLDING_REGISTERS


class FakePortLoadClient:
    """Acknowledges every write and answers a read with `firmware_version`; with `error` set, or the
    link down, a call fails unrecorded."""

    def __init__(self) -> None:
        self.error: Optional[Exception] = None
        self.firmware_version = "1.2.3"
        self._mqtt_dispatcher: Optional[MQTTDispatcher] = None
        self._writes: asyncio.Queue[dict] = asyncio.Queue()

    async def start(self, mqtt_dispatcher: MQTTDispatcher) -> None:
        self._mqtt_dispatcher = mqtt_dispatcher

    async def stop(self) -> None:
        pass

    async def call(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self, driver: str, service: str, method: str, params: dict, timeout_s: float
    ) -> RpcResult:
        del driver, service, method, timeout_s
        if self.error is not None:
            raise self.error
        if self._mqtt_dispatcher is not None and not self._mqtt_dispatcher.connected:
            raise BrokerDisconnectedError("Broker disconnected")
        if params["function"] == MODBUS_READ_HOLDING_REGISTERS:
            return {"response": "".join(f"{ord(char):04x}" for char in self.firmware_version)}
        self._writes.put_nowait(params)
        return {"response": ""}

    async def wait_for_write(self, timeout_s: float = 1.0) -> dict:
        """The params of the next write not yet taken."""
        return await asyncio.wait_for(self._writes.get(), timeout_s)

    def pending_writes(self) -> int:
        return self._writes.qsize()

    def clear(self) -> None:
        while not self._writes.empty():
            self._writes.get_nowait()
