"""wb-mqtt-serial, as far as the daemon can tell, in front of the simulated modules.

On a controller the daemon never touches Modbus: wb-mqtt-serial owns the port,
answers ``config/Load`` with the device list, executes ``port/Load`` register
writes on the daemon's behalf, and polls the gateway's reply registers and bus
monitor ring — publishing each as a control of the gateway's MQTT device
(``bus_N_bulk_send_reply_I``, ``bus_N_monitor_sporadic_frame_R``, the layout of
the WB-DALI template). This class does the same against
:class:`~wb.mqtt_dali.sim.network.SimulatedModbusNetwork`, so the unmodified
daemon and its :class:`~wb.mqtt_dali.wbdali.WBDALIDriver` run end to end with no
hardware and no wb-mqtt-serial.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..mqtt_dispatcher import get_str_payload
from ..wbdali_registers import (
    BUS_ADDRESS_OFFSET,
    MONITOR_REGISTERS_PER_SLOT,
    MONITOR_RING_SIZE,
    QUEUE_BASE,
    QUEUE_SIZE,
    from_monitor_registers,
    monitor_address,
    reply_address,
)
from .broker import Broker, Client, Message
from .network import SimulatedModbusNetwork

logger = logging.getLogger("wb.mqtt_dali.sim.serial_service")

SERIAL_RPC_PREFIX = "/rpc/v1/wb-mqtt-serial"
CONFIG_LOAD_TOPIC = f"{SERIAL_RPC_PREFIX}/config/Load"
PORT_LOAD_TOPIC = f"{SERIAL_RPC_PREFIX}/port/Load"
BUS_NUMBERS = (1, 2, 3)


def _registers_from_hex(msg: str) -> List[int]:
    """The register words of a ``port/Load`` write, in the order they hit the device."""
    return [int(msg[i : i + 4], 16) for i in range(0, len(msg) - len(msg) % 4, 4)]


def _hex_from_registers(registers: List[int]) -> str:
    return "".join(f"{register & 0xFFFF:04x}" for register in registers)


class FakeWbMqttSerial:  # pylint: disable=too-many-instance-attributes
    """Serves the daemon's wb-mqtt-serial RPCs from the simulated network.

    :param serial_config: what ``config/Load`` answers — the daemon discovers
        its WB-DALI modules from it (see :func:`~wb.mqtt_dali.sim.scenario.serial_config`)
    :param poll_interval_s: how often the monitor rings are read, like the
        template's sporadic polling
    """

    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        broker: Broker,
        network: SimulatedModbusNetwork,
        serial_config: Dict[str, Any],
        poll_interval_s: float = 0.05,
        client_id: str = "wb-mqtt-serial",
    ) -> None:
        self.broker = broker
        self.network = network
        self.serial_config = serial_config
        self.poll_interval_s = poll_interval_s
        self.client = Client(broker, client_id)
        self._tasks: List[asyncio.Task] = []
        self._published_replies: Dict[Tuple[str, int, int], int] = {}
        self._published_monitor: Dict[Tuple[str, int, int], int] = {}

    @property
    def device_ids(self) -> List[str]:
        """The modules ``config/Load`` lists — the ones whose rings get polled."""
        return [
            device["id"]
            for port in self.serial_config.get("ports", [])
            for device in port.get("devices", [])
            if "id" in device
        ]

    async def start(self) -> None:
        """Announce the endpoints, publish the initial control values, start serving."""
        await self.client.__aenter__()  # pylint: disable=unnecessary-dunder-call
        await self.client.subscribe(CONFIG_LOAD_TOPIC + "/+")
        await self.client.subscribe(PORT_LOAD_TOPIC + "/+")
        # wb-mqtt-serial marks each endpoint it serves; the daemon refuses to
        # start until config/Load carries the mark.
        self.broker.publish(CONFIG_LOAD_TOPIC, "1", qos=1, retain=True)
        self.broker.publish(PORT_LOAD_TOPIC, "1", qos=1, retain=True)
        # wb-mqtt-serial publishes every control once at start; a daemon that
        # subscribes later sees those as retained replay and ignores them.
        for device_id in self.device_ids:
            for bus in BUS_NUMBERS:
                await self._publish_replies(device_id, reply_address(bus, 0), 0)
        self._tasks = [
            asyncio.create_task(self._serve(), name="wb-mqtt-serial-rpc"),
            asyncio.create_task(self._poll_monitor_rings(), name="wb-mqtt-serial-monitor"),
        ]

    async def stop(self) -> None:
        """Stop serving and polling."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.client.__aexit__(None, None, None)  # pylint: disable=unnecessary-dunder-call

    # -- RPC --------------------------------------------------------------

    async def _serve(self) -> None:
        async for message in self.client.messages:
            try:
                await self._answer(message)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("Answering %s failed", message.topic.value)

    async def _answer(self, message: Message) -> None:
        topic = message.topic.value
        try:
            request = json.loads(get_str_payload(message))
        except ValueError:
            logger.error("Malformed request on %s: %r", topic, message.payload)
            return
        if topic.startswith(CONFIG_LOAD_TOPIC + "/"):
            result: Dict[str, Any] = {"config": self.serial_config}
        elif topic.startswith(PORT_LOAD_TOPIC + "/"):
            result = await self._port_load(request.get("params") or {})
        else:
            return
        self.broker.publish(topic + "/reply", json.dumps({"id": request.get("id"), "result": result}), qos=2)

    async def _port_load(self, params: Dict[str, Any]) -> Dict[str, Any]:
        device_id = params["device_id"]
        function = int(params["function"])
        address = int(params["address"])
        if function in (3, 4):
            registers = await self.network.read_input(device_id, address, int(params.get("count", 1)))
            return {"response": _hex_from_registers(registers)}
        if function in (6, 16):
            registers = _registers_from_hex(params.get("msg") or "")
            await self.network.write_holding(device_id, address, registers)
            await self._publish_replies(device_id, address, len(registers))
            return {"response": ""}
        raise ValueError(f"unsupported Modbus function {function}")

    # -- what wb-mqtt-serial's polling would publish -------------------------

    async def _publish_replies(self, device_id: str, address: int, register_count: int) -> None:
        """Publish the reply registers a write may have changed.

        wb-mqtt-serial polls them and publishes on change. A slot's reply is
        cleared the moment the slot is written and set again when the frame
        has gone out, so every written slot gets a publication even when the
        new answer equals the old — that transition is what a poller sees.
        """
        bus = (address - QUEUE_BASE) // BUS_ADDRESS_OFFSET + 1
        if bus not in BUS_NUMBERS:
            return
        local = address - (bus - 1) * BUS_ADDRESS_OFFSET
        written = set()
        if QUEUE_BASE <= local < QUEUE_BASE + QUEUE_SIZE * 2:
            first_slot = (local - QUEUE_BASE) // 2
            written = set(range(first_slot, min(first_slot + max(1, register_count // 2), QUEUE_SIZE)))
        replies = await self.network.read_input(device_id, reply_address(bus, 0), QUEUE_SIZE)
        for slot, value in enumerate(replies):
            key = (device_id, bus, slot)
            if slot not in written and self._published_replies.get(key) == value:
                continue
            self._published_replies[key] = value
            self.broker.publish(
                f"/devices/{device_id}/controls/bus_{bus}_bulk_send_reply_{slot}",
                str(value),
                qos=1,
                retain=True,
            )

    async def _poll_monitor_rings(self) -> None:
        count = MONITOR_RING_SIZE * MONITOR_REGISTERS_PER_SLOT
        while True:
            await asyncio.sleep(self.poll_interval_s)
            for device_id in self.device_ids:
                for bus in BUS_NUMBERS:
                    try:
                        registers = await self.network.read_input(device_id, monitor_address(bus, 0), count)
                    except Exception:  # pylint: disable=broad-exception-caught
                        continue  # an unreachable module answers nothing, like the real one
                    for slot in range(MONITOR_RING_SIZE):
                        words = registers[
                            slot * MONITOR_REGISTERS_PER_SLOT : (slot + 1) * MONITOR_REGISTERS_PER_SLOT
                        ]
                        raw = from_monitor_registers(words)
                        key = (device_id, bus, slot)
                        if raw == 0 or self._published_monitor.get(key) == raw:
                            continue
                        self._published_monitor[key] = raw
                        self.broker.publish(
                            f"/devices/{device_id}/controls/bus_{bus}_monitor_sporadic_frame_{slot + 1}",
                            str(raw),
                            qos=1,
                            retain=True,
                        )


def default_serial_config(
    device_ids: List[str], slave_ids: Optional[Dict[str, int]] = None
) -> Dict[str, Any]:
    """A wb-mqtt-serial config listing the given modules as enabled WB-DALI devices."""
    return {
        "ports": [
            {
                "path": "/dev/ttyRS485-1",
                "enabled": True,
                "devices": [
                    {
                        "id": device_id,
                        "slave_id": (slave_ids or {}).get(device_id, index + 1),
                        "device_type": "WB-DALI",
                        "enabled": True,
                    }
                    for index, device_id in enumerate(device_ids)
                ],
            }
        ]
    }
