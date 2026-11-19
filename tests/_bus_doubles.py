"""Doubles for tests of a running bus: a command-level DALI bus in place of WBDALIDriver, a bus
started on it with its MQTT recorded, and a device factory."""

import asyncio
import contextlib
import logging
from typing import AsyncIterator, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock

import aiomqtt
from dali.address import GearShort
from dali.command import Command, Response
from dali.frame import BackwardFrame
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
from wb.mqtt_dali.bus_traffic import BusTrafficCallbacks, BusTrafficSource
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress
from wb.mqtt_dali.gateway import bus_from_json
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import BusDeviceInstanceTypeMapper, WBDALIConfig

from ._broker_link_helpers import RecordingClient
from ._common_schema import load_common_schema

GATEWAY_ID = "gw1"
BUS_INDEX = 1
BUS_UID = f"{GATEWAY_ID}_bus_{BUS_INDEX}"
BUS_LOGGER = logging.getLogger(BUS_UID)

# Non-zero query answers: QueryDeviceType 254 = "no extended device types",
# QueryVersionNumber 2 = modern gear (non-legacy memory bank layout), fade time/rate 0x01 =
# no fade at rate 1 (0 is not a legal rate), QueryFeatureType 254 = "no DALI-2 feature types".
_QUERY_RESPONSES = {
    "QueryDeviceType": 254,
    "QueryVersionNumber": 2,
    "QueryFadeTimeFadeRate": 0x01,
    "QueryFeatureType": 254,
}

# Virtual time for a started bus to initialise its gear.
INIT_S = 1.0
# Virtual time for queued controller work to run out; shorter than any poll or fade settle.
SETTLE_S = 0.05


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


def make_device(device_class, short: int, mqtt_id: Optional[str] = None):
    device = device_class(
        DaliDeviceAddress(short=short, random=0),
        BUS_UID,
        MagicMock(),
        load_common_schema(),
        mqtt_id,
    )
    device.set_logger(BUS_LOGGER)
    return device


class FakeWBDALIDriver:  # pylint: disable=too-many-instance-attributes
    """Fake WBDALIDriver over a bus of gear: a query answers a fixed byte per command type, group
    membership follows AddToGroup/RemoveFromGroup, an empty bus answers nothing."""

    def __init__(
        self,
        answers: Optional[Dict[str, int]] = None,
        groups: Optional[Dict[int, Set[int]]] = None,
        empty_bus: bool = False,
    ) -> None:
        self.config = WBDALIConfig(device_name=GATEWAY_ID, bus=BUS_INDEX)
        self.logger = BUS_LOGGER
        # Backward-frame byte per query command class name; anything not listed answers 0.
        self.answers = {**_QUERY_RESPONSES, **(answers or {})}
        self.groups = {short: set(numbers) for short, numbers in (groups or {}).items()}
        self.empty_bus = empty_bus
        self.sent: List[Command] = []
        self.bus_traffic = BusTrafficCallbacks(WBDALIConfig().queue_size)
        self.dev_inst_map = BusDeviceInstanceTypeMapper()
        self._sequence_id = 0

    async def initialize(self) -> None:
        pass

    async def deinitialize(self) -> None:
        pass

    def set_bus_monitor_enabled(self, enabled: bool) -> None:
        pass

    def set_has_control_devices(self, present: bool) -> None:
        pass

    async def send(self, cmd, source=None, priority=None, logger=None):
        del priority, logger
        self.sent.append(cmd)
        response = self._response_to(cmd)
        if cmd.devicetype != 0:
            self.report(EnableDeviceType(cmd.devicetype), Response(None), source)
        self.report(cmd, response, source)
        return response

    async def send_commands(self, cmds, source=None, priority=None, logger=None):
        return [await self.send(cmd, source, priority, logger) for cmd in cmds]

    async def run_sequence(self, seq, priority=None, logger=None):
        """Mirror WBDALIDriver.run_sequence's generator protocol."""
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
                response = await self._answer(cmd, priority, logger)
        finally:
            seq.close()

    def report(self, cmd: Command, response: Response, source=None) -> None:
        """Put an exchange into bus traffic the way WBDALIDriver does, failed ones included."""
        self.bus_traffic.notify_command(cmd.frame, response, source or BusTrafficSource.WB, self._sequence_id)
        self._sequence_id += 1

    # --- Private ---

    async def _answer(self, cmd, priority, logger):
        if isinstance(cmd, list):
            return await self.send_commands(cmd, None, priority, logger)
        return await self.send(cmd, None, priority, logger)

    def _response_to(self, cmd: Command) -> Response:
        if self.empty_bus:
            return cmd.response(None) if cmd.response is not None else Response(None)
        destination = getattr(cmd, "destination", None)
        if isinstance(destination, GearShort):
            self._record_group_change(cmd, destination.address)
        if cmd.response is None:
            return Response(None)
        if isinstance(cmd, (QueryGroupsZeroToSeven, QueryGroupsEightToFifteen)) and isinstance(
            destination, GearShort
        ):
            return cmd.response(BackwardFrame(self._group_byte(cmd, destination.address)))
        return cmd.response(BackwardFrame(self.answers.get(type(cmd).__name__, 0)))

    def _record_group_change(self, cmd: Command, short: int) -> None:
        if isinstance(cmd, AddToGroup):
            self.groups.setdefault(short, set()).add(cmd.param)
        elif isinstance(cmd, RemoveFromGroup):
            self.groups.setdefault(short, set()).discard(cmd.param)

    def _group_byte(self, cmd: Command, short: int) -> int:
        first = 0 if isinstance(cmd, QueryGroupsZeroToSeven) else 8
        members = self.groups.get(short, set())
        return sum(1 << (number - first) for number in members if first <= number < first + 8)


@contextlib.asynccontextmanager
async def running_bus(
    data: dict, driver: FakeWBDALIDriver
) -> AsyncIterator[Tuple[ApplicationController, RecordingClient]]:
    """A bus built from its config entry on ``driver`` and started, its MQTT under a real dispatcher."""
    client = RecordingClient()
    dispatcher = MQTTDispatcher(client)
    dispatch_task = asyncio.create_task(dispatcher.run())
    bus = make_bus(data, driver, dispatcher)
    await bus.start()
    try:
        yield bus, client
    finally:
        await bus.stop()
        dispatch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dispatch_task


async def settle_init(client: RecordingClient, driver: FakeWBDALIDriver) -> None:
    """Let the bus initialise its gear, then forget the startup traffic."""
    await asyncio.sleep(INIT_S)
    client.clear()
    driver.sent.clear()


def make_bus(data: dict, driver: FakeWBDALIDriver, dispatcher=None) -> ApplicationController:
    return bus_from_json(
        data,
        dispatcher or MagicMock(),
        driver,
        MagicMock(),
        load_common_schema(),
        MagicMock(),
    )


def write(client: RecordingClient, device_id: str, control_id: str, payload: str) -> None:
    topic = f"/devices/{device_id}/controls/{control_id}/on"
    client.messages.deliver(aiomqtt.Message(topic, payload, 0, False, 0, None))


def payloads(client: RecordingClient, topic: str) -> List[Optional[str]]:
    """Payloads published on ``topic``, in order; ``None`` is the topic cleared."""
    return [publish.payload for publish in client.publishes if publish.topic == topic]


def is_published(client: RecordingClient, topic: str) -> bool:
    return payloads(client, topic)[-1:] not in ([], [None])


def control_values(client: RecordingClient, device_id: str, control_id: str) -> List[Optional[str]]:
    return payloads(client, f"/devices/{device_id}/controls/{control_id}")


def control_errors(client: RecordingClient, device_id: str, control_id: str) -> List[Optional[str]]:
    return payloads(client, f"/devices/{device_id}/controls/{control_id}/meta/error")


def control_error(client: RecordingClient, device_id: str, control_id: str) -> Optional[str]:
    errors = control_errors(client, device_id, control_id)
    return errors[-1] if errors else None
