"""End-to-end coverage that each call path picks the right FramePriority.

The driver builds the Modbus payload from per-command priority and publishes
it via MQTT; tests parse the hex payload from the RPC envelope to recover the
priority bits actually placed on the wire. No private access to driver state
is required beyond what existing tests already use — ``MockMqttClient.wait_for_publish``
is the observability point.

Priority bit layout (encoded register, bits [31..29]):
  TRANSACTION_CONTINUATION = 1 -> 0x20000000
  USER_ACTION              = 2 -> 0x40000000
  CONFIGURATION            = 3 -> 0x60000000
  AUTOMATIC                = 4 -> 0x80000000
  PERIODIC_QUERY           = 5 -> 0xA0000000
"""

# pylint: disable=redefined-outer-name

import asyncio
import json
import logging
from typing import NamedTuple, Optional
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
from dali.address import GearGroup, GearShort
from dali.command import Command
from dali.frame import ForwardFrame
from dali.gear.colour import QueryColourValue, StoreColourTemperatureTcLimit
from dali.gear.general import (
    DAPC,
    DTR0,
    DTR1,
    DTR2,
    EnableDeviceType,
    GoToLastActiveLevel,
    Off,
    QueryActualLevel,
    QueryContentDTR0,
    ReadMemoryLocation,
    SetFadeTime,
    SetScene,
)
from dali.memory import info

try:
    from pytest_asyncio import fixture as pytest_asyncio_fixture
except ImportError:  # pytest-asyncio < 0.17 (system Debian bullseye)
    pytest_asyncio_fixture = pytest.fixture

from tests.test_commissioning import FakeDALIBus
from wb.mqtt_dali.bus_traffic import BusTrafficSource
from wb.mqtt_dali.commissioning import Commissioning, check_presence
from wb.mqtt_dali.common_dali_device import ControlInfo, MqttControl, read_memory_bank
from wb.mqtt_dali.dali_common_parameters import FadeTimeFadeRateParam
from wb.mqtt_dali.dali_compat import DaliCommandsCompatibilityLayer
from wb.mqtt_dali.dali_device import query_device_types_sequence
from wb.mqtt_dali.dali_type8_parameters import ColourType, query_colour_with_level
from wb.mqtt_dali.mqtt_dispatcher import MQTTDispatcher
from wb.mqtt_dali.wbdali import FramePriority, WBDALIConfig, WBDALIDriver
from wb.mqtt_dali.wbdali_utils import send_commands_with_retry
from wb.mqtt_dali.wbmqtt import ControlMeta

_RPC_LOAD_TOPIC = "/rpc/v1/wb-mqtt-serial/port/Load/{client_id}"

_TEST_LOGGER = logging.getLogger("test_frame_priority")
_TEST_LOGGER.setLevel(logging.CRITICAL + 1)


class _MockMqttClient:
    def __init__(self):
        self._client = MagicMock()
        self._client._client_id = "test-frame-priority-client"
        self._messages = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def subscribe(self, topic):
        del topic

    async def publish(self, topic, payload):
        await self._messages.put((topic, payload))

    async def clear_publishes(self):
        while not self._messages.empty():
            await self._messages.get()

    async def wait_for_publish(self, topic, timeout=1.0):
        while True:
            try:
                published_topic, message = await asyncio.wait_for(self._messages.get(), timeout=timeout)
                if published_topic == topic:
                    return message
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timeout waiting for publish to {topic}") from None


class _RecordedFrame(NamedTuple):
    """One frame as it appeared on the Modbus payload — priority + raw bits."""

    raw: int
    priority_value: int
    frame_data: int

    @classmethod
    def from_raw32(cls, raw32: int) -> "_RecordedFrame":
        return cls(raw=raw32, priority_value=(raw32 >> 29) & 0x7, frame_data=raw32 & 0x1FFFFFF)


def _decode_payload(payload_str: str) -> list[_RecordedFrame]:
    """Decode the wb-mqtt-serial RPC envelope and return a list of frames.

    The envelope's ``msg`` field is hex-encoded 16-bit Modbus registers in
    word-swapped order (little-endian word pair per 32-bit register, the same
    transformation ``WBDALIDriver._send_to_gateway`` applies on the way out).
    """
    payload = json.loads(payload_str)
    hex_msg = payload["params"]["msg"]
    frames: list[_RecordedFrame] = []
    for i in range(0, len(hex_msg), 8):
        word_swapped = int(hex_msg[i : i + 8], 16)
        # Undo the high/low word swap from _send_to_gateway.
        unswapped = ((word_swapped & 0xFFFF) << 16) | ((word_swapped >> 16) & 0xFFFF)
        frames.append(_RecordedFrame.from_raw32(unswapped))
    return frames


class _MockCommand(Command):
    """16-bit command with optional response — kept simple for traffic-shape tests."""

    def __init__(self, data=None, response_class: Optional[type] = None) -> None:
        if data is None:
            data = [0x12, 0x34]
        super().__init__(ForwardFrame(16, data))
        self.sendtwice = False
        self.response = response_class

    def __str__(self) -> str:
        return "_MockCommand"


def _simulate_reply(dispatcher: MQTTDispatcher, config: WBDALIConfig, index: int) -> None:
    """Inject a `transmission without response` reply for the given queue slot."""
    topic = f"/devices/{config.device_name}/controls/" f"bus_{config.bus}_bulk_send_reply_{index}".encode()
    message = mqtt.MQTTMessage(topic=topic)
    message.payload = str(0x0200).encode()  # status 2: transmission without response
    dispatcher._dispatch_message(message)  # pylint: disable=protected-access


def _simulate_reply_with_response(
    dispatcher: MQTTDispatcher, config: WBDALIConfig, index: int, backward_byte: int = 0
) -> None:
    """Inject a `transmission with backward response` reply for the given queue slot."""
    topic = f"/devices/{config.device_name}/controls/" f"bus_{config.bus}_bulk_send_reply_{index}".encode()
    message = mqtt.MQTTMessage(topic=topic)
    message.payload = str(0x0100 | (backward_byte & 0xFF)).encode()
    dispatcher._dispatch_message(message)  # pylint: disable=protected-access


@pytest_asyncio_fixture
async def initialized_driver():
    mqtt_client = _MockMqttClient()
    dispatcher = MQTTDispatcher(mqtt_client)
    driver = WBDALIDriver(WBDALIConfig(), dispatcher, _TEST_LOGGER)
    await driver.initialize()
    await mqtt_client.clear_publishes()
    try:
        yield driver, mqtt_client, dispatcher
    finally:
        await driver.deinitialize()


async def _cancel_background_tasks(*tasks: asyncio.Task) -> None:
    """Cancel and await a set of background tasks, swallowing the CancelledError."""
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _capture_one_batch(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    driver: WBDALIDriver,
    mqtt_client: _MockMqttClient,
    dispatcher: MQTTDispatcher,
    coro,
    expected_frames: int,
    with_response: bool = False,
) -> list[_RecordedFrame]:
    """Drive ``coro`` until one batch is published, reply to each slot and return parsed frames."""
    fut = asyncio.create_task(coro)
    payload = await mqtt_client.wait_for_publish(topic=_RPC_LOAD_TOPIC.format(client_id=driver.rpc_client_id))
    for index in range(expected_frames):
        if with_response:
            _simulate_reply_with_response(dispatcher, driver.config, index)
        else:
            _simulate_reply(dispatcher, driver.config, index)
    await fut
    return _decode_payload(payload)


# --- Low-level: encoded priority bits ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "priority,expected_priority_bits",
    [
        (FramePriority.USER_ACTION, 2),
        (FramePriority.CONFIGURATION, 3),
        (FramePriority.AUTOMATIC, 4),
        (FramePriority.PERIODIC_QUERY, 5),
    ],
)
async def test_send_commands_priority_propagates_to_payload(
    initialized_driver, priority, expected_priority_bits
):
    """Each FramePriority value reaches the encoded Modbus payload unchanged for a single-frame send."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver, mqtt_client, dispatcher, driver.send(_MockCommand(), priority=priority), 1
    )
    assert len(frames) == 1
    assert frames[0].priority_value == expected_priority_bits


# --- Caller-intent priority (first frame / single-frame) ---


@pytest.mark.asyncio
async def test_mqtt_level_write_uses_user_action_priority(initialized_driver):
    """A control-topic-style DAPC write executed via ``send_commands_with_retry``
    (the path taken by ``CommonDaliDevice.execute_control``) lands on the wire
    with USER_ACTION priority."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        send_commands_with_retry(driver, [DAPC(GearShort(0), 128)]),
        1,
    )
    assert len(frames) == 1
    assert frames[0].priority_value == FramePriority.USER_ACTION.value


@pytest.mark.asyncio
async def test_polling_loop_uses_periodic_query_priority(initialized_driver):
    """An ``MqttControl`` polled through its public ``next_poll_step`` factory
    (the entry-point that ``PollScheduler.poll`` invokes inside
    ``_polling_loop``) sends its query at PERIODIC_QUERY priority. We exercise
    the returned ``poll_coroutine`` so the assertion covers the actual on-wire
    frame produced by the polling code path, not a stub."""
    driver, mqtt_client, dispatcher = initialized_driver
    control = MqttControl(
        control_info=ControlInfo(id="level", meta=ControlMeta(control_type="value", read_only=True)),
        query_builder=QueryActualLevel,
        value_formatter=lambda r: str(r.raw_value.as_integer if r.raw_value is not None else ""),
    )
    step = control.next_poll_step(
        driver=driver,
        address=GearShort(0),
        max_commands=1,
        default_max_commands=3,
        now=0.0,
    )
    assert step.poll_coroutine is not None
    frames = await _capture_one_batch(
        driver, mqtt_client, dispatcher, step.poll_coroutine(), 1, with_response=True
    )
    assert len(frames) == 1
    assert frames[0].priority_value == FramePriority.PERIODIC_QUERY.value


@pytest.mark.asyncio
async def test_parameter_write_uses_configuration_priority(initialized_driver):
    """A parameter handler ``write`` (here: ``FadeTimeFadeRateParam`` on a single
    device, which sends DTR0+SetFadeTime+DTR0+SetFadeRate+QueryFadeTimeFadeRate)
    starts with CONFIGURATION priority on the leading DTR0; subsequent frames
    are auto-promoted to TRANSACTION_CONTINUATION by the DTR/EDT rules in
    ``_send_commands_internal``."""
    driver, mqtt_client, dispatcher = initialized_driver
    param = FadeTimeFadeRateParam()
    observed: list[int] = []
    publisher_task = asyncio.create_task(_collect_priorities(mqtt_client, driver, observed))
    replier_task = asyncio.create_task(_keep_replying(dispatcher, driver.config))
    try:
        await param.write(driver, GearShort(0), {"fade_time": 4, "fade_rate": 3}, MagicMock())
    finally:
        await _cancel_background_tasks(publisher_task, replier_task)
    assert observed, "no frames captured"
    # First frame is always caller's CONFIGURATION; subsequent ones are
    # TRANSACTION_CONTINUATION because each follows a DTR0 set or is a
    # DTR-using setter/query.
    assert observed[0] == FramePriority.CONFIGURATION.value
    promoted = [p for p in observed[1:] if p == FramePriority.TRANSACTION_CONTINUATION.value]
    assert promoted, f"expected continuations after first frame, got {observed}"


@pytest.mark.asyncio
async def test_parameter_read_in_ui_flow_uses_configuration_priority(initialized_driver):
    """``FadeTimeFadeRateParam.read`` — exercised end-to-end against the real
    driver — emits a single ``QueryFadeTimeFadeRate`` at CONFIGURATION priority.
    Asserts on the wire frame produced by the parameter handler itself, not on
    the underlying retry helper."""
    driver, mqtt_client, dispatcher = initialized_driver
    param = FadeTimeFadeRateParam()
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        param.read(driver, GearShort(0), MagicMock()),
        1,
        with_response=True,
    )
    assert len(frames) == 1
    assert frames[0].priority_value == FramePriority.CONFIGURATION.value


@pytest.mark.asyncio
async def test_apply_group_parameters_uses_configuration_priority(initialized_driver):
    """``handler.write`` on a group address (the layer exercised by
    ``ApplicationController._apply_group_parameters_task``) starts with
    CONFIGURATION on the first frame. The dtr-led frames that follow
    auto-promote — but the *intent* priority of the batch is CONFIGURATION,
    asserted on the leading DTR0."""
    driver, mqtt_client, dispatcher = initialized_driver
    param = FadeTimeFadeRateParam()
    observed: list[int] = []
    publisher_task = asyncio.create_task(_collect_priorities(mqtt_client, driver, observed))
    replier_task = asyncio.create_task(_keep_replying(dispatcher, driver.config))
    try:
        await param.write(driver, GearGroup(0), {"fade_time": 4, "fade_rate": 3}, MagicMock())
    finally:
        await _cancel_background_tasks(publisher_task, replier_task)
    assert observed
    assert observed[0] == FramePriority.CONFIGURATION.value


@pytest.mark.asyncio
async def test_commissioning_uses_user_action_priority(initialized_driver):
    """Every leading frame in commissioning batches carries USER_ACTION priority.
    Exercises ``check_presence`` — the public commissioning entry-point that
    sends ``[Terminate, Initialise, SetSearchAddr×3, Compare]`` followed by a
    trailing ``Terminate`` in finally. None of these declare DTR/EDT structure,
    so every frame is a leading frame and must be USER_ACTION."""
    driver, mqtt_client, dispatcher = initialized_driver
    observed: list[int] = []
    publisher_task = asyncio.create_task(_collect_priorities(mqtt_client, driver, observed))
    replier_task = asyncio.create_task(_keep_replying_no_response(dispatcher, driver.config))
    try:
        await check_presence(driver, dali2=False)
    finally:
        await _cancel_background_tasks(publisher_task, replier_task)
    assert observed, "commissioning produced no frames"
    non_continuation = [p for p in observed if p != FramePriority.TRANSACTION_CONTINUATION.value]
    assert non_continuation, "all frames were continuations — unexpected"
    assert all(p == FramePriority.USER_ACTION.value for p in non_continuation), non_continuation


@pytest.mark.asyncio
async def test_commissioning_smart_extend_uses_user_action_priority():
    """``Commissioning.smart_extend`` exercises the binary-search path —
    ``QueryControlGearPresent``, ``QueryRandomAddress*``, ``SetSearchAddr*``,
    ``Compare``, ``Initialise``, ``Randomise``, ``ProgramShortAddress``,
    ``Withdraw``, ``Terminate``. Wraps ``FakeDALIBus`` to record the
    ``priority`` argument on every ``send``/``send_commands`` call; asserts
    every recorded priority is ``USER_ACTION``."""
    inner = FakeDALIBus(devices={0: 0x123456, 1: 0x789ABC})
    recorded: list[Optional[FramePriority]] = []

    class _RecordingBus:
        async def send(self, cmd, source=BusTrafficSource.WB, priority=None):
            recorded.append(priority)
            return await inner.send(cmd, source=source, priority=priority)

        async def send_commands(self, cmds, source=BusTrafficSource.WB, priority=None):
            recorded.append(priority)
            return await inner.send_commands(cmds, source=source, priority=priority)

    commissioning = Commissioning(_RecordingBus(), [])
    await commissioning.smart_extend()
    assert recorded, "smart_extend produced no driver calls"
    assert all(p == FramePriority.USER_ACTION for p in recorded), recorded


# --- Auto-promotion in _send_commands_internal ---


@pytest.mark.asyncio
async def test_first_frame_always_caller_priority(initialized_driver):
    """Even when the batch starts with DTR0 (a DTR-set), frame 0 keeps the
    caller's priority — auto-promotion only applies from frame 1 onward."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands([DTR0(5)], priority=FramePriority.CONFIGURATION),
        1,
    )
    assert len(frames) == 1
    assert frames[0].priority_value == FramePriority.CONFIGURATION.value


@pytest.mark.asyncio
async def test_dtr_chain_promotes_after_first(initialized_driver):
    """``[DTR0, DTR1, DTR2]`` @ CONFIG produces priorities 3, 1, 1 — each DTR
    after the first is preceded by another DTR set, so it auto-promotes."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands([DTR0(1), DTR1(2), DTR2(3)], priority=FramePriority.CONFIGURATION),
        3,
    )
    assert [f.priority_value for f in frames] == [
        FramePriority.CONFIGURATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
    ]


@pytest.mark.asyncio
async def test_dtr_consumer_after_dtr_set_promoted(initialized_driver):
    """``[DTR0, SetFadeTime]`` @ CONFIG produces 3, 1 — SetFadeTime follows a DTR set."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands([DTR0(5), SetFadeTime(GearShort(0))], priority=FramePriority.CONFIGURATION),
        2,
    )
    assert [f.priority_value for f in frames] == [
        FramePriority.CONFIGURATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
    ]


@pytest.mark.asyncio
async def test_uses_dtr_promoted_regardless_of_prev(initialized_driver):
    """``[QueryColourValue, QueryContentDTR0]`` @ CONFIG → with EDT auto-prefix:
    ``[EDT(8), QueryColourValue, QueryContentDTR0]``. QueryContentDTR0 has
    ``uses_dtr0=True`` so it auto-promotes even though the previous frame
    (QueryColourValue) is neither a DTR set nor EDT — covers the
    ``commands[i].uses_dtr*`` arm. Mirrors ``dali_type8_tc.py:253``."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands(
            [QueryColourValue(GearShort(0)), QueryContentDTR0(GearShort(0))],
            priority=FramePriority.CONFIGURATION,
        ),
        3,
        with_response=True,
    )
    # EDT(8) auto-inserted because QueryColourValue.devicetype == 8.
    assert len(frames) == 3
    assert [f.priority_value for f in frames] == [
        FramePriority.CONFIGURATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
    ]


@pytest.mark.asyncio
async def test_dt_cmd_after_auto_edt_promoted(initialized_driver):
    """A DT-typed command sent solo with USER_ACTION → EDT auto-inserted as
    frame 0 at USER_ACTION; the DT command becomes frame 1 and auto-promotes
    because it follows EnableDeviceType."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send(QueryColourValue(GearShort(0))),
        2,
        with_response=True,
    )
    assert len(frames) == 2
    assert frames[0].frame_data == EnableDeviceType(8).frame.as_integer
    assert frames[0].priority_value == FramePriority.USER_ACTION.value
    assert frames[1].priority_value == FramePriority.TRANSACTION_CONTINUATION.value


@pytest.mark.asyncio
async def test_dtr_chain_with_edt_all_continuations(initialized_driver):
    """``[DTR0, DTR1, DTR2, StoreColourTemperatureTcLimit]`` @ CONFIG.
    StoreColourTemperatureTcLimit is a DT8 command so EDT(8) is auto-inserted
    before it. Expected priorities: 3, 1, 1, 1, 1 (one frame per command plus
    the EDT prefix). Mirrors ``dali_type8_tc.py:340``."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands(
            [DTR0(0), DTR1(0), DTR2(0), StoreColourTemperatureTcLimit(GearShort(0))],
            priority=FramePriority.CONFIGURATION,
        ),
        5,
    )
    assert [f.priority_value for f in frames] == [
        FramePriority.CONFIGURATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
    ]


@pytest.mark.asyncio
async def test_read_memory_location_chain_promoted(initialized_driver):
    """``[DTR1, DTR0, RML, RML]`` @ PERIODIC_QUERY → 5, 1, 1, 1 — covers the
    DT51 active-energy polling pattern in ``dali_type51_parameters.py``."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands(
            [
                DTR1(202),
                DTR0(0x05),
                ReadMemoryLocation(GearShort(0)),
                ReadMemoryLocation(GearShort(0)),
            ],
            priority=FramePriority.PERIODIC_QUERY,
        ),
        4,
        with_response=True,
    )
    assert [f.priority_value for f in frames] == [
        FramePriority.PERIODIC_QUERY.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
    ]


@pytest.mark.asyncio
async def test_unrelated_commands_not_promoted(initialized_driver):
    """``[GoToLastActiveLevel, Off]`` @ USER_ACTION → 2, 2. An RPC batch of
    plain control commands is not a transaction; no auto-promotion."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands([GoToLastActiveLevel(GearShort(0)), Off(GearShort(1))]),
        2,
    )
    assert [f.priority_value for f in frames] == [
        FramePriority.USER_ACTION.value,
        FramePriority.USER_ACTION.value,
    ]


@pytest.mark.asyncio
async def test_multi_segment_dtr_partial_promotion(initialized_driver):
    """``[DTR0(s0), SetScene(0), DTR0(s1), SetScene(1)]`` @ CONFIG → 3, 1, 3, 1.
    Documents the gap between segments in Scenes-write
    (``dali_common_parameters.py:389``): SetScene does not declare
    ``uses_dtr0`` so the second DTR0 is treated as the start of a fresh
    segment rather than a continuation of the previous one."""
    driver, mqtt_client, dispatcher = initialized_driver
    frames = await _capture_one_batch(
        driver,
        mqtt_client,
        dispatcher,
        driver.send_commands(
            [
                DTR0(0x80),
                SetScene(GearShort(0), 0),
                DTR0(0x81),
                SetScene(GearShort(0), 1),
            ],
            priority=FramePriority.CONFIGURATION,
        ),
        4,
    )
    assert [f.priority_value for f in frames] == [
        FramePriority.CONFIGURATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
        FramePriority.CONFIGURATION.value,
        FramePriority.TRANSACTION_CONTINUATION.value,
    ]


# --- run_sequence: each yield is a separate _send_commands_internal call ---


@pytest.mark.asyncio
async def test_run_sequence_query_device_types_caller_prio(initialized_driver):
    """``query_device_types_sequence`` issues one ``QueryDeviceType`` followed by
    a chain of ``QueryNextDeviceType`` driven by per-yield responses. Each yield
    is a separate ``_send_commands_internal`` call, so every frame is i=0 in its
    own call — caller's USER_ACTION priority, no cross-yield promotion.
    Scripted response chain: first byte 255 (=> enter the loop), then a real
    type code (5), then 254 (=> terminate), producing 3 outgoing frames."""
    driver, mqtt_client, dispatcher = initialized_driver
    script = [(0x01, 255), (0x01, 5), (0x01, 254)]
    observed: list[int] = []
    pump_task = asyncio.create_task(
        _pump_publishes_and_reply_scripted(mqtt_client, dispatcher, driver, script, observed)
    )
    try:
        result = await driver.run_sequence(query_device_types_sequence(GearShort(0)))
    finally:
        await _cancel_background_tasks(pump_task)
    assert result == [5]
    assert observed == [FramePriority.USER_ACTION.value] * 3, observed


@pytest.mark.asyncio
async def test_run_sequence_read_memory_bank_caller_prio(initialized_driver):
    """``read_memory_bank`` runs ``LastAddress.read`` (3 single-frame yields:
    DTR1, DTR0, ReadMemoryLocation) and then 2 more yields (DTR0 + a list of
    ReadMemoryLocation). Each yield is its own ``_send_commands_internal``
    call — the leading frame of every batch is caller's CONFIGURATION priority.
    Scripted to make ``LastAddress.read`` return 3 (so bank 0 has 2 readable
    locations 2..3), then two RML data bytes back-to-back."""
    driver, mqtt_client, dispatcher = initialized_driver
    script = [
        (0x02, 0),  # DTR1 (LastAddress.read)
        (0x02, 0),  # DTR0 (LastAddress.read)
        (0x01, 3),  # ReadMemoryLocation -> last_address = 3
        (0x02, 0),  # DTR0 (read_memory_bank start_address)
        (0x01, 0xAA),  # RML[0]
        (0x01, 0xBB),  # RML[1]
    ]
    observed: list[int] = []
    pump_task = asyncio.create_task(
        _pump_publishes_and_reply_scripted(
            mqtt_client, dispatcher, driver, script, observed, batch_first_only=True
        )
    )
    try:
        await driver.run_sequence(
            read_memory_bank(info.BANK_0, GearShort(0), DaliCommandsCompatibilityLayer()),
            priority=FramePriority.CONFIGURATION,
        )
    finally:
        await _cancel_background_tasks(pump_task)
    assert observed, "no batches captured"
    assert all(p == FramePriority.CONFIGURATION.value for p in observed), observed


@pytest.mark.asyncio
async def test_run_sequence_query_colour_with_level_caller_prio(initialized_driver):
    """``query_colour_with_level`` first batch is ``[QueryActualLevel, DTR0,
    QueryColourValue]``. The driver auto-inserts ``EnableDeviceType(8)`` before
    the DT8 ``QueryColourValue``, so 4 frames go on the wire. Replying with
    MASK (255) as the colour-type makes the sequence early-return after one
    batch — exactly one yield, so only one ``_send_commands_internal`` call.
    Asserts the leading frame carries the caller's CONFIGURATION priority."""
    driver, mqtt_client, dispatcher = initialized_driver
    script = [
        (0x01, 50),  # QueryActualLevel -> level=50
        (0x01, 0),  # DTR0
        (0x01, 0),  # auto-inserted EnableDeviceType(8)
        (0x01, 255),  # QueryColourValue -> colour_type = MASK
    ]
    observed: list[int] = []
    pump_task = asyncio.create_task(
        _pump_publishes_and_reply_scripted(
            mqtt_client, dispatcher, driver, script, observed, batch_first_only=True
        )
    )
    try:
        addr = GearShort(0)
        await driver.run_sequence(
            query_colour_with_level(addr, QueryActualLevel(addr), {}, ColourType.COLOUR_TEMPERATURE),
            priority=FramePriority.CONFIGURATION,
        )
    finally:
        await _cancel_background_tasks(pump_task)
    assert observed, "no batches captured"
    assert all(p == FramePriority.CONFIGURATION.value for p in observed), observed


# --- Helpers shared by the multi-frame tests ---


async def _collect_priorities(mqtt_client: _MockMqttClient, driver: WBDALIDriver, sink: list[int]) -> None:
    """Continuously decode published payloads and append every priority value
    seen into ``sink``. Cancelled by the test once the under-test coroutine
    completes."""
    topic = _RPC_LOAD_TOPIC.format(client_id=driver.rpc_client_id)
    while True:
        payload = await mqtt_client.wait_for_publish(topic=topic, timeout=2.0)
        for frame in _decode_payload(payload):
            sink.append(frame.priority_value)


async def _keep_replying(dispatcher: MQTTDispatcher, config: WBDALIConfig) -> None:
    """Background task: feed back a successful reply for every queue slot in
    round-robin order. Cancelled when the test finishes."""
    slot = 0
    while True:
        await asyncio.sleep(0.005)
        # Inject "transmission with backward response" for whichever slot is
        # currently waiting — driver only ever processes a recognised slot.
        topic = (
            f"/devices/{config.device_name}/controls/"
            f"bus_{config.bus}_bulk_send_reply_{slot % 16}".encode()
        )
        message = mqtt.MQTTMessage(topic=topic)
        message.payload = str(0x0100).encode()
        dispatcher._dispatch_message(message)  # pylint: disable=protected-access
        slot += 1


async def _keep_replying_no_response(dispatcher: MQTTDispatcher, config: WBDALIConfig) -> None:
    """Like ``_keep_replying`` but reports status=2 (transmission without
    response) — simulates an empty bus where no gear responds."""
    slot = 0
    while True:
        await asyncio.sleep(0.005)
        topic = (
            f"/devices/{config.device_name}/controls/"
            f"bus_{config.bus}_bulk_send_reply_{slot % 16}".encode()
        )
        message = mqtt.MQTTMessage(topic=topic)
        message.payload = str(0x0200).encode()
        dispatcher._dispatch_message(message)  # pylint: disable=protected-access
        slot += 1


async def _pump_publishes_and_reply_scripted(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    mqtt_client: _MockMqttClient,
    dispatcher: MQTTDispatcher,
    driver: WBDALIDriver,
    script: list[tuple[int, int]],
    observed_priorities: list[int],
    batch_first_only: bool = False,
) -> None:
    """Single pump task: read each batch published by the driver, record either
    every priority or only each batch's leading frame into
    ``observed_priorities``, and post a scripted reply for every slot in the
    batch. ``script`` is a list of ``(status, backward_byte)`` per slot; slots
    beyond the script get status=2 (transmission without response). Splitting
    observe + reply between two coroutines would race for the same publish
    queue and lose messages; combining them keeps each publish on one thread.
    """
    topic = _RPC_LOAD_TOPIC.format(client_id=driver.rpc_client_id)
    slot = 0
    script_index = 0
    while True:
        decoded = _decode_payload(await mqtt_client.wait_for_publish(topic=topic, timeout=2.0))
        if batch_first_only:
            observed_priorities.append(decoded[0].priority_value)
        else:
            observed_priorities.extend(f.priority_value for f in decoded)
        for _ in decoded:
            status, backward = script[script_index] if script_index < len(script) else (0x02, 0)
            script_index += 1
            message = mqtt.MQTTMessage(
                topic=(
                    f"/devices/{driver.config.device_name}/controls/"
                    f"bus_{driver.config.bus}_bulk_send_reply_{slot % 16}".encode()
                )
            )
            message.payload = str((status << 8) | (backward & 0xFF)).encode()
            dispatcher._dispatch_message(message)  # pylint: disable=protected-access
            slot += 1
