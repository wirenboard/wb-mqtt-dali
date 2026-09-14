"""Fixtures for the on_off feature tests: a scripted fake DALI driver, and the same answers
one level down -- a scripted gateway on MQTT that a real driver and a real bus run against."""

import asyncio
import json
from dataclasses import dataclass, field
from enum import IntEnum
from timeit import default_timer
from typing import Callable, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple
from unittest.mock import MagicMock

import aiomqtt
from dali.address import GearGroup, GearShort
from dali.command import Command, Response, from_frame
from dali.frame import BackwardFrame, ForwardFrame
from dali.gear.general import (
    AddToGroup,
    EnableDeviceType,
    QueryGroupsEightToFifteen,
    QueryGroupsZeroToSeven,
    RemoveFromGroup,
)
from dali.sequences import progress as seq_progress
from dali.sequences import sleep as seq_sleep

from wb.mqtt_dali.application_controller import ApplicationController
from wb.mqtt_dali.gateway import bus_from_json
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import WBDALIConfig

from ._broker_link_helpers import Publish, RecordingClient

# Non-zero query answers: QueryDeviceType 254 = "no extended device types",
# QueryVersionNumber 2 = modern gear (non-legacy memory bank layout), fade time/rate 0x01 =
# no fade at rate 1 (0 is not a legal rate).
_QUERY_RESPONSES = {"QueryDeviceType": 254, "QueryVersionNumber": 2, "QueryFadeTimeFadeRate": 0x01}


def frames(commands) -> list:
    """Commands as (name, frame) pairs, so an assertion compares payloads, not objects."""
    return [(type(command).__name__, command.frame.as_integer) for command in commands]


def group_settings(bus) -> dict:
    """The on/off settings a bus would write to its config, by group number."""
    return {
        number: device.on_off_config
        for number, device in bus.group_devices.items()
        if device.on_off_config is not None
    }


class ScriptedDriver:
    """Fake WBDALIDriver answering every query with a fixed per-command-type byte."""

    def __init__(self):
        self.sent = []

    async def send(self, cmd, source=None, priority=None):
        del source, priority
        self.sent.append(cmd)
        if cmd.response is None:
            return Response(None)
        return cmd.response(BackwardFrame(_QUERY_RESPONSES.get(type(cmd).__name__, 0)))

    async def send_commands(self, cmds, source=None, priority=None):
        return [await self.send(cmd, source, priority) for cmd in cmds]

    async def run_sequence(self, seq, priority=None, progress=None):
        """Mirror WBDALIDriver.run_sequence's generator protocol."""
        del priority, progress
        response = None
        started = False
        try:
            while True:
                try:
                    cmd = next(seq) if not started else seq.send(response)
                    started = True
                except StopIteration as stop:
                    return stop.value
                response = Response(None)
                if isinstance(cmd, (seq_sleep, seq_progress)):
                    continue
                response = await self._answer(cmd)
        finally:
            seq.close()

    async def _answer(self, cmd):
        if isinstance(cmd, list):
            return await self.send_commands(cmd)
        return await self.send(cmd)


_LOAD_TOPIC_PREFIX = "/rpc/v1/wb-mqtt-serial/port/Load/"
_MODBUS_WRITE_REGISTERS = 16
_FRAME_BITS_BY_SIZE_CODE = {0: 16, 1: 24, 2: 25}
_DECODABLE_FRAME_BITS = (16, 24)


class ReplyStatus(IntEnum):
    """wb-mqtt-serial reply status, bits [15..8] of a bulk_send_reply payload."""

    WITH_RESPONSE = 1
    WITHOUT_RESPONSE = 2
    NO_POWER_ON_BUS = 4


@dataclass
class BusScript:
    """The bus the scripted gateway pretends to have behind the wb-mqtt-serial queue."""

    # Backward-frame byte per query command class name; anything not listed answers 0.
    answers: Dict[str, int] = field(default_factory=dict)
    # Group membership per short address; AddToGroup/RemoveFromGroup move it like real gear.
    groups: Dict[int, Set[int]] = field(default_factory=dict)
    # The gear on the bus; ``None`` means every address answers. Gear that is not there stays
    # silent, so a device configured at its short address never initializes.
    present_shorts: Optional[FrozenSet[int]] = None
    # Groups whose frames the gateway reports as untransmittable, so group writes fail.
    dead_groups: FrozenSet[int] = frozenset()
    # Query command class names the gear leaves unanswered, so their controls stay unread.
    unanswered: FrozenSet[str] = frozenset()


class ScriptedGatewayClient(RecordingClient):
    """Client double that answers the frames the driver loads into the gateway queue.

    Each frame of a queue write is answered on its reply topic, so a real WBDALIDriver
    over a real MQTTDispatcher carries real devices through initialize() with no bus.
    """

    def __init__(self, script: BusScript, device_name: str, bus: int) -> None:
        super().__init__()
        self._script = script
        self._config = WBDALIConfig(device_name=device_name, bus=bus)
        self._devicetype = 0
        self._groups = {short: set(numbers) for short, numbers in script.groups.items()}
        self.sent: List[Command] = []

    async def publish(
        self, topic: str, payload: Optional[str] = None, qos: int = 0, retain: bool = False
    ) -> None:
        await super().publish(topic, payload, qos, retain)
        if topic.startswith(_LOAD_TOPIC_PREFIX) and payload is not None:
            self._answer_queue_write(payload)

    # --- Private ---

    def _answer_queue_write(self, payload: str) -> None:
        params = json.loads(payload)["params"]
        if params["function"] != _MODBUS_WRITE_REGISTERS:
            return  # the queue pointer reset, no frames to answer
        start_slot = (
            params["address"]
            - self._config.queue_start_modbus_address
            - (self._config.bus - 1) * self._config.queue_modbus_bus_offset
        ) // 2
        msg = params["msg"]
        for position in range(0, len(msg), 8):
            word_swapped = int(msg[position : position + 8], 16)
            register = ((word_swapped & 0xFFFF) << 16) | (word_swapped >> 16)
            slot = start_slot + position // 8
            topic = (
                f"/devices/{self._config.device_name}/controls/"
                f"bus_{self._config.bus}_bulk_send_reply_{slot}"
            )
            reply = self._reply_for(register)
            self.messages.deliver(aiomqtt.Message(topic, str(reply), 0, False, 0, None))

    def _reply_for(self, register: int) -> int:
        bits = _FRAME_BITS_BY_SIZE_CODE[(register >> 25) & 0x7]
        command = None
        if bits in _DECODABLE_FRAME_BITS:
            command = from_frame(ForwardFrame(bits, register & 0x1FFFFFF), devicetype=self._devicetype)
        # An EnableDeviceType prefix decides how the next frame decodes.
        self._devicetype = command.param if isinstance(command, EnableDeviceType) else 0
        if command is None:
            return ReplyStatus.WITHOUT_RESPONSE << 8
        self.sent.append(command)
        destination = getattr(command, "destination", None)
        if isinstance(destination, GearGroup) and destination.group in self._script.dead_groups:
            return ReplyStatus.NO_POWER_ON_BUS << 8
        if not self._is_answered(destination) or type(command).__name__ in self._script.unanswered:
            return ReplyStatus.WITHOUT_RESPONSE << 8
        if isinstance(destination, GearShort):
            self._record_group_change(command, destination.address)
        if command.response is None:
            return ReplyStatus.WITHOUT_RESPONSE << 8
        if isinstance(command, (QueryGroupsZeroToSeven, QueryGroupsEightToFifteen)) and isinstance(
            destination, GearShort
        ):
            return (ReplyStatus.WITH_RESPONSE << 8) | self._group_byte(command, destination.address)
        answers = {**_QUERY_RESPONSES, **self._script.answers}
        return (ReplyStatus.WITH_RESPONSE << 8) | answers.get(type(command).__name__, 0)

    def _record_group_change(self, command: Command, short: int) -> None:
        if isinstance(command, AddToGroup):
            self._groups.setdefault(short, set()).add(command.param)
        elif isinstance(command, RemoveFromGroup):
            self._groups.setdefault(short, set()).discard(command.param)

    def _group_byte(self, command: Command, short: int) -> int:
        first = 0 if isinstance(command, QueryGroupsZeroToSeven) else 8
        members = self._groups.get(short, set())
        return sum(1 << (number - first) for number in members if first <= number < first + 8)

    def _is_answered(self, destination) -> bool:
        present = self._script.present_shorts
        if present is None:
            return True
        if isinstance(destination, GearShort):
            return destination.address in present
        # Broadcast and group queries: whoever is out there answers, an empty bus does not.
        return bool(present)


# A settle round must outlast the driver's 10 ms batch flush, or a step's traffic
# would not have reached the client before the round is called quiet.
_SETTLE_ROUND_S = 0.01
_SETTLE_QUIET_ROUNDS = 3
_POLL_ROUND_S = 0.005


class ScriptedBus:
    """A scripted DALI gateway under a real MQTTDispatcher: buses talk MQTT and nothing else.

    Async context manager; the buses started through it are stopped on the way out.
    """

    def __init__(
        self, script: Optional[BusScript] = None, gateway_id: str = "gw1", bus_index: int = 1
    ) -> None:
        self.client = ScriptedGatewayClient(script or BusScript(), gateway_id, bus_index)
        self.dispatcher = MQTTDispatcher(self.client)
        self._gateway_id = gateway_id
        self._bus_index = bus_index
        self._dispatch_task: Optional[asyncio.Task] = None
        self._started: List[ApplicationController] = []

    async def __aenter__(self) -> "ScriptedBus":
        self._dispatch_task = asyncio.create_task(self.dispatcher.run())
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        for controller in reversed(self._started):
            await controller.stop()
        self._started.clear()
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None

    async def start(self, controller: ApplicationController) -> ApplicationController:
        await controller.start()
        self._started.append(controller)
        return controller

    async def start_bus(self, data: dict) -> ApplicationController:
        """Build a bus from its config entry over this gateway and start it."""
        return await self.start(
            bus_from_json(self._gateway_id, self._bus_index, data, self.dispatcher, MagicMock())
        )

    async def write(self, device_id: str, control_id: str, payload: str) -> None:
        """Write to a control's /on topic and wait for the controller to be done with it."""
        topic = f"/devices/{device_id}/controls/{control_id}/on"
        seen = len(self.client.calls)
        self.client.messages.deliver(aiomqtt.Message(topic, payload, 0, False, 0, None))
        # The frames or the write error: a write the controller acted on always says something.
        await self.wait_until(lambda: len(self.client.calls) > seen)
        await self.settle()

    async def settle(self) -> None:
        """Let the queued controller work run until the client sees no new MQTT calls."""
        seen = len(self.client.calls)
        quiet = 0
        while quiet < _SETTLE_QUIET_ROUNDS:
            await asyncio.sleep(_SETTLE_ROUND_S)
            if len(self.client.calls) == seen:
                quiet += 1
            else:
                seen = len(self.client.calls)
                quiet = 0

    async def wait_until(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = default_timer() + timeout
        while not predicate():
            if default_timer() > deadline:
                raise TimeoutError("Condition not met in time")
            await asyncio.sleep(_POLL_ROUND_S)

    def drain(self) -> None:
        """Forget the publishes and frames so far, so an assertion can scope itself."""
        self.client.clear()
        self.client.sent.clear()

    @property
    def sent(self) -> List[Command]:
        """The commands the driver has put on the bus."""
        return self.client.sent

    def published_devices(self) -> Set[str]:
        published = {}
        for publish in self.client.publishes:
            parts = publish.topic.split("/")
            if len(parts) == 4 and parts[1] == "devices" and parts[3] == "meta":
                published[parts[2]] = publish.payload is not None
        return {device_id for device_id, present in published.items() if present}

    def device_publishes(self, device_id: str) -> List[Optional[str]]:
        """The payloads published on a device's meta topic; ``None`` is that device removed."""
        return [
            publish.payload
            for publish in self.client.publishes
            if publish.topic == f"/devices/{device_id}/meta"
        ]

    def published_controls(self, device_id: str) -> Set[str]:
        published = {}
        for control_id, publish in self._control_publishes(device_id, "meta"):
            published[control_id] = publish.payload is not None
        return {control_id for control_id, present in published.items() if present}

    def values(self, device_id: str) -> Dict[str, Optional[str]]:
        """The last value published per control of the device."""
        return {control_id: publish.payload for control_id, publish in self._control_publishes(device_id)}

    def value_publishes(self, device_id: str, control_id: str) -> List[Optional[str]]:
        return [
            publish.payload
            for published_id, publish in self._control_publishes(device_id)
            if published_id == control_id
        ]

    def control_error(self, device_id: str, control_id: str) -> Optional[str]:
        errors = [
            publish.payload
            for published_id, publish in self._control_publishes(device_id, "meta/error")
            if published_id == control_id
        ]
        return errors[-1] if errors else None

    # --- Private ---

    def _control_publishes(self, device_id: str, suffix: str = "") -> Iterator[Tuple[str, Publish]]:
        """(control_id, publish) for every publish on the device's control topics."""
        prefix = f"/devices/{device_id}/controls/"
        for publish in self.client.publishes:
            if not publish.topic.startswith(prefix):
                continue
            tail = publish.topic[len(prefix) :]
            control_id, _, publish_suffix = tail.partition("/")
            if publish_suffix == suffix:
                yield control_id, publish
