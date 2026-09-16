"""Initial control-state read on device init + value preservation on rebuild.

Covers the three scenarios of ``docs/init_control_state_plan.md``: the pre-publish read and
seed, what a rebuild carries over, and how a group takes its state from a member.
"""

import logging
from dataclasses import dataclass
from timeit import default_timer
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.gear.colour import tc_kelvin_mirek
from dali.gear.general import QueryActualLevel

from wb.mqtt_dali.application_controller import try_initialize_device
from wb.mqtt_dali.common_dali_device import (
    PERIODIC_STATUS_POLL_INTERVAL,
    ControlsPollRequestResult,
    DaliDeviceAddress,
    DaliDeviceBase,
    EventPollSchedule,
    MqttControl,
    MqttControlBase,
    NotifyResult,
    SingleQueryControl,
)
from wb.mqtt_dali.control_ids import ACTUAL_LEVEL, CURRENT_COLOUR_TEMPERATURE
from wb.mqtt_dali.control_ids import DAPC as DAPC_ID
from wb.mqtt_dali.control_ids import SET_COLOUR_TEMPERATURE, WANTED_LEVEL
from wb.mqtt_dali.dali_controls import (
    ActualLevelControl,
    DapcControl,
    WantedLevelControl,
)
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.dali_type8_common import ColourComponent
from wb.mqtt_dali.dali_type8_tc import (
    CurrentColourTemperatureControl,
    SetColourTemperatureControl,
)
from wb.mqtt_dali.device_init_scheduler import DeviceInitScheduler
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.events import BusEvent, ColourChanged, EventSource
from wb.mqtt_dali.wbmqtt import ControlError, ControlMeta, ControlState, TranslatedTitle

from ._app_controller_helpers import make_group_controller

# Prevent file-system access in DaliDeviceBase.__init__ (repo-wide test hack).
DaliDeviceBase._common_schema = {"title": "test-schema", "properties": {}}  # pylint: disable=protected-access

_LOGGER = logging.getLogger("test.init_control_state")


# ---------------------------------------------------------------------------
# Device / control / publisher helpers
# ---------------------------------------------------------------------------


class _StubDevice(DaliDevice):
    """A real ``DaliDevice`` with an injected control set.

    Only the two builder hooks are overridden, so init itself runs exactly as it ships;
    ``extra_pollables`` stands in for the chunked handlers a real DT8/DT51 device contributes."""

    def __init__(self, controls_factory, extra_pollables=(), **kwargs):
        self._controls_factory = controls_factory
        self._extra_pollables = list(extra_pollables)
        super().__init__(**kwargs)

    def _build_mqtt_controls(self):
        # First called from init right after the type loop, so this is also where a DT8/DT51
        # handler would have contributed its chunked pollable.
        self._standalone_pollables = list(self._extra_pollables)
        return self._controls_factory()


def _make_stub_device(controls_factory, short=1, extra_pollables=()):
    return _StubDevice(
        controls_factory,
        extra_pollables=extra_pollables,
        address=DaliDeviceAddress(short=short, random=0x00),
        bus_id="bus",
        gtin_db=MagicMock(),
    )


@dataclass(frozen=True)
class _StubRead(BusEvent):
    """A stub control's read outcome, addressed by control id so several stubs on one device do
    not react to each other's reads."""

    control_id: str
    value: Optional[str] = None
    title: Optional[TranslatedTitle] = None
    failed: bool = False


def _apply_stub_read(control: MqttControlBase, event: BusEvent) -> NotifyResult:
    """The stub controls' shared ``notify``: the state is written here, as on every real control."""
    if not isinstance(event, _StubRead) or event.control_id != control.control_info.id:
        return NotifyResult.NOTHING_TO_PUBLISH
    if event.failed:
        control.control_info.state.error = ControlError.READ
        return NotifyResult.PUBLISH_STATE
    control.control_info.state.value = event.value
    if event.title is not None:
        control.control_info.state.meta.title = event.title
    control.control_info.state.error = ControlError.NONE
    return NotifyResult.PUBLISH_STATE


class _ReadableStub(SingleQueryControl):
    """Readable control reporting the values it was given, the last one repeating for ever.

    A ``None`` among them stands for a read the gear did not answer. ``title`` makes it an alarm
    control, whose title is read together with the value.
    """

    def __init__(self, control_info, values, title=None):
        super().__init__(control_info, query_builder=lambda _address: MagicMock())
        self._values = list(values)
        self._title = title

    def decode_response(self, response):
        value = self._values.pop(0) if len(self._values) > 1 else self._values[0]
        if response is None or value is None:
            return _StubRead(self.control_info.id, failed=True)
        return _StubRead(self.control_info.id, value, self._title)

    def notify(self, event):
        return _apply_stub_read(self, event)


class _MirroredStub(MqttControlBase):
    """Control with no read of its own whose value follows another pollable's readback — the
    shape of a DT8 colour control fed by its type handler's read cycle."""

    def notify(self, event):
        return _apply_stub_read(self, event)


def _control_info(control_id, default="default", control_type="value", title=None, **meta):
    return ControlInfo(
        control_id,
        ControlState(
            ControlMeta(control_type, title or TranslatedTitle(control_id, control_id), **meta), default
        ),
    )


def _readable_control(control_id, value, default="default"):
    """Readable control whose read always reports ``value``."""
    return _ReadableStub(_control_info(control_id, default), values=[value])


def _failing_readable_control(control_id, default="default"):
    """Readable control the gear never answers, so every read of it fails."""
    return _ReadableStub(_control_info(control_id, default), values=[None])


def _flaky_readable_control(control_id, value, default="default"):
    """Readable control whose first read fails and every later one reports ``value``."""
    return _ReadableStub(_control_info(control_id, default), values=[None, value])


_ALARM_FAULT_TITLE = TranslatedTitle("Lamp failure", "Неисправность лампы")


def _alarm_control(control_id):
    """Readable alarm control whose read carries the fault title along with the value."""
    return _ReadableStub(
        _control_info(control_id, "0", "alarm", title=TranslatedTitle("Ok", "Норма")),
        values=["1"],
        title=_ALARM_FAULT_TITLE,
    )


def _mirrored_control(control_id, default="default"):
    """Target of a chunked pollable's readback: no read of its own, state written by ``notify``."""
    return _MirroredStub(_control_info(control_id, default))


def _tc_controls():
    """The real DT8 colour-temperature pair: the state control and the setpoint mirroring it."""
    return [
        CurrentColourTemperatureControl(),
        SetColourTemperatureControl(tc_kelvin_mirek(6500), tc_kelvin_mirek(2000)),
    ]


def _tc_read(mirek, failed=False):
    """One colour read cycle's outcome, the event the DT8 handler emits."""
    return ColourChanged({ColourComponent.COLOUR_TEMPERATURE: mirek}, EventSource.READ, failed=failed)


class _ChunkedPollable(EventPollSchedule):
    """Multi-tick pollable serving one step per tick, like the DT51 energy / DT8 colour reads.

    A raising step leaves the cycle open, as the real chunked handlers do; ``_SAFETY_LIMIT``
    closes it anyway, so a regression that re-offers the step fails an assertion, not spins."""

    _SAFETY_LIMIT = 5

    def __init__(self, steps):
        super().__init__(PERIODIC_STATUS_POLL_INTERVAL, randomize_poll_interval=False)
        self.attempts = 0
        self._steps = list(steps)
        self._served = 0
        self._in_cycle = False

    def is_poll_due(self, now):
        return self._in_cycle or super().is_poll_due(now)

    def has_in_progress_read(self) -> bool:
        """Whether a cycle is still open — same public observable as the DT51 handler."""
        return self._in_cycle

    def next_poll_step(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, driver, address, max_commands, default_max_commands, now, logger=None
    ):
        del driver, address, max_commands, default_max_commands, logger
        if not self.is_poll_due(now):
            return ControlsPollRequestResult(has_more=False)
        if not self._in_cycle:
            self._in_cycle = True
            self.schedule_next_periodic_poll(polled_at=now)
        step = self._steps[self._served]
        return ControlsPollRequestResult(
            has_more=self._served < len(self._steps) - 1,
            poll_coroutine=lambda: self._serve(step),
            commands_count=1,
        )

    def cancel_pending_poll(self):
        self._in_cycle = False

    async def _serve(self, step):
        self.attempts += 1
        if self.attempts >= self._SAFETY_LIMIT:
            self._in_cycle = False
        if isinstance(step, Exception):
            raise step
        self._served += 1
        if self._served == len(self._steps):
            self._in_cycle = False
        return [step]


class _BrokenPollable(_ChunkedPollable):
    """Pollable that fails while *planning* its step, not while reading."""

    def next_poll_step(self, *args, **kwargs):
        raise RuntimeError("bus exploded")


def _pushbutton_control(control_id):
    """Write-only pushbutton — not readable, no query, no meaningful state."""
    return MqttControl(
        ControlInfo(
            control_id, ControlState(ControlMeta("pushbutton", TranslatedTitle(control_id, control_id)))
        ),
        commands_builder=lambda addr, val: [],
    )


def _setpoint_control(control_id, default, minimum=None, maximum=None):
    """Control with no read of its own; the optional bounds exercise the rebuild's fit check."""
    return MqttControl(
        ControlInfo(
            control_id,
            ControlState(
                ControlMeta(
                    "value",
                    TranslatedTitle(control_id, control_id),
                    minimum=minimum,
                    maximum=maximum,
                ),
                default,
            ),
        ),
        commands_builder=lambda addr, val: [],
    )


def _make_publisher():
    pub = MagicMock()
    for coroutine_method in (
        "add_device",
        "remove_device",
        "register_control_handler",
        "set_control_value",
        "set_control_title",
        "set_control_error",
    ):
        setattr(pub, coroutine_method, AsyncMock())
    pub.has_device = MagicMock(return_value=False)
    return pub


def _ok_response(raw_value=0):
    resp = MagicMock()
    resp.raw_value = MagicMock()
    resp.raw_value.error = False
    resp.raw_value.as_integer = raw_value
    # check_query_response reads this attr; a bare MagicMock would be truthy.
    resp._error_acceptable = False  # pylint: disable=protected-access
    return resp


def _unanswered_response():
    """Response of a gear that did not answer: ``check_query_response`` rejects it."""
    resp = MagicMock()
    resp.raw_value = None
    resp._error_acceptable = False  # pylint: disable=protected-access
    return resp


def _make_driver(raw_value=0):
    """Driver reporting no device types and answering every query with ``raw_value``."""

    async def fake_run_sequence(seq, priority=None):
        del priority
        name = seq.gi_code.co_name
        seq.close()
        if name == "query_device_types_sequence":
            return []
        raise AssertionError(f"unexpected run_sequence target: {name}")

    async def fake_send_commands(cmds, source=None, priority=None):
        del source, priority
        return [_ok_response(raw_value) for _ in cmds]

    driver = AsyncMock()
    driver.run_sequence = AsyncMock(side_effect=fake_run_sequence)
    driver.send_commands = AsyncMock(side_effect=fake_send_commands)
    return driver


def _make_driver_silent_on(command_type):
    """Driver answering everything except queries of ``command_type``, as silent gear does."""

    async def fake_send_commands(cmds, source=None, priority=None):
        del source, priority
        return [_unanswered_response() if isinstance(cmd, command_type) else _ok_response() for cmd in cmds]

    driver = _make_driver()
    driver.send_commands = AsyncMock(side_effect=fake_send_commands)
    return driver


def _published_controls(publisher):
    """Map of control id -> published value from the last add_device call."""
    device_info = publisher.add_device.await_args.args[0]
    return {c.id: c.state.value for c in device_info.controls}


def _published_titles(publisher):
    """Map of control id -> published meta title from the last add_device call."""
    device_info = publisher.add_device.await_args.args[0]
    return {c.id: c.state.meta.title for c in device_info.controls}


def _published_errors(publisher):
    """Map of control id -> published /meta/error from the last add_device call."""
    device_info = publisher.add_device.await_args.args[0]
    return {c.id: c.state.error for c in device_info.controls}


# ---------------------------------------------------------------------------
# Scenario 1 — initial read at device init
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_publishes_read_control_values_on_init():
    """A readable control's current value is read via the poll machinery before the first
    publish, so the device is published carrying the real value, not the builder default."""
    publisher = _make_publisher()
    device = _make_stub_device(lambda: [_readable_control("illuminance", "77", default="0")])

    result = await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    assert result is True
    assert _published_controls(publisher)["illuminance"] == "77"


@pytest.mark.asyncio
async def test_failed_initial_read_publishes_error_r():
    """A control whose initial read fails carries ControlError.READ in the single add_device
    publish, and the device still initializes."""
    publisher = _make_publisher()
    device = _make_stub_device(lambda: [_failing_readable_control("illuminance")])

    result = await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    assert result is True
    assert _published_errors(publisher)["illuminance"] == ControlError.READ
    publisher.set_control_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_read_failure_does_not_fail_device_init():
    """When some controls read and others fail, the device initializes successfully (its
    scheduler records success, not a retry) — only a genuine init failure fails init."""
    publisher = _make_publisher()
    device = _make_stub_device(
        lambda: [_readable_control("illuminance", "5"), _failing_readable_control("temperature")]
    )
    scheduler = DeviceInitScheduler()
    scheduler.schedule(device.mqtt_id, 0.0)

    result = await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        scheduler,
        MagicMock(),
        _LOGGER,
        0.0,
    )

    assert result is True
    assert not scheduler.has_pending  # record_success removed it; no retry scheduled
    published_errors = _published_errors(publisher)
    assert published_errors["temperature"] == ControlError.READ
    assert published_errors["illuminance"] == ControlError.NONE
    assert _published_controls(publisher)["illuminance"] == "5"


@pytest.mark.asyncio
async def test_colour_temperature_setpoint_published_from_read_not_default():
    """set_colour_temperature has no read of its own: the colour read cycle's event reaches it
    like any other control, so the 4000 K default is published nowhere at all."""
    publisher = _make_publisher()
    device = _make_stub_device(
        _tc_controls,
        extra_pollables=[_ChunkedPollable([_tc_read(200)])],  # 200 mirek = 5000 K
    )

    await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    published = _published_controls(publisher)
    assert published[SET_COLOUR_TEMPERATURE] == "5000"
    assert "4000" not in published.values()
    publisher.set_control_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_state_published_only_via_add_device():
    """The 'no flash' guarantee: read value, derived setpoint and read error all ride out in the
    single add_device publish, with no set_control_value/set_control_error during init."""
    publisher = _make_publisher()
    device = _make_stub_device(
        lambda: [*_tc_controls(), _failing_readable_control("temperature")],
        extra_pollables=[_ChunkedPollable([_tc_read(200)])],  # 200 mirek = 5000 K
    )

    await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    publisher.set_control_value.assert_not_awaited()
    publisher.set_control_error.assert_not_awaited()
    published = _published_controls(publisher)
    assert published[CURRENT_COLOUR_TEMPERATURE] == "5000"
    assert published[SET_COLOUR_TEMPERATURE] == "5000"
    assert _published_errors(publisher)["temperature"] == ControlError.READ


@pytest.mark.asyncio
async def test_non_readable_controls_not_read_and_not_errored():
    """A pushbutton (write-only) control is neither read nor given a read error at init: only
    readable controls are polled, so the button keeps its placeholder and gets no /meta/error."""
    publisher = _make_publisher()
    device = _make_stub_device(lambda: [_readable_control("illuminance", "12"), _pushbutton_control("off")])

    await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    assert device.get_mqtt_control("off").control_info.state.value is None
    assert _published_errors(publisher).get("off") == ControlError.NONE
    error_targets = [call.args[1] for call in publisher.set_control_error.await_args_list]
    assert "off" not in error_targets


@pytest.mark.asyncio
async def test_initial_read_counts_as_the_first_poll_of_each_control():
    """The read moves each control's schedule on, so the poll round right after publication does
    not read everything a second time."""
    publisher = _make_publisher()
    device = _make_stub_device(lambda: [_readable_control("illuminance", "5", default="0")])

    await try_initialize_device(
        device, _make_driver(), publisher, DeviceInitScheduler(), MagicMock(), _LOGGER, 0.0
    )

    assert device.get_mqtt_control("illuminance").time_until_next_poll(default_timer()) > 0


@pytest.mark.asyncio
async def test_a_later_poll_heals_a_failed_initial_read():
    """The failed initial read leaves the read error standing on the control, and the poll that
    succeeds clears it and brings its value along — the same dispatch path, later."""
    device = _make_stub_device(lambda: [_flaky_readable_control("illuminance", "42", default="0")])

    await try_initialize_device(
        device, _make_driver(), _make_publisher(), DeviceInitScheduler(), MagicMock(), _LOGGER, 0.0
    )

    control = device.get_mqtt_control("illuminance")
    assert control.control_info.state.error == ControlError.READ
    assert control.control_info.state.value == "0"

    now = default_timer()
    step = device.poll_controls(_make_driver(), now + control.time_until_next_poll(now), 100, 100)
    for event in await step.poll_coroutine():
        device.notify_all(event)

    assert control.control_info.state.error == ControlError.NONE
    assert control.control_info.state.value == "42"


@pytest.mark.asyncio
async def test_initial_read_drains_all_readable_controls():
    """Every readable control is queried once, so all their values are published (not just the
    first)."""
    publisher = _make_publisher()
    device = _make_stub_device(
        lambda: [
            _readable_control("illuminance", "11", default="0"),
            _readable_control("temperature", "22", default="0"),
            _pushbutton_control("off"),
        ]
    )

    await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    published = _published_controls(publisher)
    assert published["illuminance"] == "11"
    assert published["temperature"] == "22"


@pytest.mark.asyncio
async def test_initial_read_drains_a_multi_tick_pollable_across_ticks():
    """A chunked pollable reports has_more until its last tick and the drain keeps going, so
    every tick's readback lands in the single publish."""
    publisher = _make_publisher()
    pollable = _ChunkedPollable(
        [_StubRead("chunk_a", "1"), _StubRead("chunk_b", "2"), _StubRead("chunk_c", "3")]
    )
    device = _make_stub_device(
        lambda: [_mirrored_control(f"chunk_{name}") for name in ("a", "b", "c")],
        extra_pollables=[pollable],
    )

    await try_initialize_device(
        device, _make_driver(), publisher, DeviceInitScheduler(), MagicMock(), _LOGGER, 0.0
    )

    assert pollable.attempts == 3
    published = _published_controls(publisher)
    assert [published["chunk_a"], published["chunk_b"], published["chunk_c"]] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_initial_read_step_that_raises_ends_the_drain_and_keeps_what_was_read():
    """A raising tick leaves its pollable mid-cycle, so re-offering it would repeat the same
    failing step forever. The drain ends there instead, keeping what earlier ticks read."""
    publisher = _make_publisher()
    pollable = _ChunkedPollable([_StubRead("chunk_a", "1"), RuntimeError("chunk exploded")])
    device = _make_stub_device(
        lambda: [_mirrored_control("chunk_a"), _mirrored_control("chunk_b")],
        extra_pollables=[pollable],
    )

    result = await try_initialize_device(
        device, _make_driver(), publisher, DeviceInitScheduler(), MagicMock(), _LOGGER, 0.0
    )

    assert result is True
    assert pollable.attempts == 2  # served once, raised once, never re-offered
    assert pollable.has_in_progress_read() is False
    published = _published_controls(publisher)
    assert published["chunk_a"] == "1"
    assert published["chunk_b"] == "default"


@pytest.mark.asyncio
async def test_level_triplet_setpoints_seeded_from_the_actual_level_read():
    """The actual_level read seeds wanted_level (rounded percent) and dapc (raw level) in the
    single publish, so neither shows its 0 builder default."""
    publisher = _make_publisher()
    curve = DimmingCurveState()
    device = _make_stub_device(
        lambda: [
            ActualLevelControl(curve),
            WantedLevelControl(curve),
            DapcControl(),
        ]
    )

    await try_initialize_device(
        device,
        _make_driver(raw_value=254),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    published = _published_controls(publisher)
    assert published[ACTUAL_LEVEL] == "100.000"
    assert published[WANTED_LEVEL] == "100"
    assert published[DAPC_ID] == "254"


@pytest.mark.asyncio
async def test_level_setpoints_not_seeded_from_a_failed_actual_level_read():
    """The gear does not answer the actual_level query: wanted_level and dapc keep their defaults
    with no error of their own, and init still succeeds."""
    publisher = _make_publisher()
    curve = DimmingCurveState()
    device = _make_stub_device(
        lambda: [
            ActualLevelControl(curve),
            WantedLevelControl(curve),
            DapcControl(),
        ]
    )

    result = await try_initialize_device(
        device,
        _make_driver_silent_on(QueryActualLevel),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    assert result is True
    published = _published_controls(publisher)
    assert published[WANTED_LEVEL] == "0"
    assert published[DAPC_ID] == "0"
    published_errors = _published_errors(publisher)
    assert published_errors[ACTUAL_LEVEL] == ControlError.READ
    assert published_errors[WANTED_LEVEL] == ControlError.NONE
    assert published_errors[DAPC_ID] == ControlError.NONE


@pytest.mark.asyncio
async def test_read_exception_does_not_fail_device_init():
    """A pollable that blows up while planning its step drops out on its own: init still
    succeeds and a control read alongside it is published with its read value."""
    publisher = _make_publisher()
    device = _make_stub_device(
        lambda: [_readable_control("illuminance", "9", default="0")],
        extra_pollables=[_BrokenPollable([])],
    )

    result = await try_initialize_device(
        device, _make_driver(), publisher, DeviceInitScheduler(), MagicMock(), _LOGGER, 0.0
    )

    assert result is True
    assert publisher.add_device.await_count == 1
    assert _published_controls(publisher)["illuminance"] == "9"  # the broken pollable took nothing


@pytest.mark.asyncio
async def test_alarm_title_seeded_into_meta_before_publish():
    """An alarm control's read yields a title, and the seed writes it into meta.title, so both
    halves ride out in the single publish instead of the "Ok" placeholder."""
    publisher = _make_publisher()
    device = _make_stub_device(lambda: [_alarm_control("alarm")])

    await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    assert _published_controls(publisher)["alarm"] == "1"
    assert _published_titles(publisher)["alarm"] == _ALARM_FAULT_TITLE


@pytest.mark.asyncio
async def test_setpoint_without_source_read_keeps_default():
    """A setpoint whose source state was not read keeps its builder default and gets no error:
    setpoints have no read of their own to fail."""
    publisher = _make_publisher()
    device = _make_stub_device(lambda: [_setpoint_control(SET_COLOUR_TEMPERATURE, "4000")])

    await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    setpoint = device.get_mqtt_control(SET_COLOUR_TEMPERATURE)
    assert setpoint.control_info.state.value == "4000"
    assert setpoint.control_info.state.error == ControlError.NONE


@pytest.mark.asyncio
async def test_setpoint_not_seeded_from_a_failed_source_read():
    """The colour read cycle fails: set_colour_temperature keeps its default without an error,
    and only the state control carries /meta/error=r."""
    publisher = _make_publisher()
    device = _make_stub_device(
        _tc_controls,
        extra_pollables=[_ChunkedPollable([_tc_read(None, failed=True)])],
    )

    result = await try_initialize_device(
        device,
        _make_driver(),
        publisher,
        DeviceInitScheduler(),
        MagicMock(),
        _LOGGER,
        0.0,
    )

    assert result is True
    assert _published_controls(publisher)[SET_COLOUR_TEMPERATURE] == "4000"
    published_errors = _published_errors(publisher)
    assert published_errors[CURRENT_COLOUR_TEMPERATURE] == ControlError.READ
    assert published_errors[SET_COLOUR_TEMPERATURE] == ControlError.NONE


# ---------------------------------------------------------------------------
# Scenario 2 — rebuild carries current state onto the rebuilt controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_after_init_builds_fresh_controls():
    """The controls that init read are handed to the *first* build only. A rebuild exists to pick
    up new meta — narrowed TC limits here — so it must get freshly built controls, not those."""
    limits = {"minimum": 2000, "maximum": 6500}
    device = _make_stub_device(lambda: [_setpoint_control(SET_COLOUR_TEMPERATURE, "4000", **limits)])
    await try_initialize_device(
        device, _make_driver(), _make_publisher(), DeviceInitScheduler(), MagicMock(), _LOGGER, 0.0
    )

    limits.update(minimum=3000, maximum=4000)
    device.rebuild_mqtt_controls()

    meta = device.get_mqtt_control(SET_COLOUR_TEMPERATURE).control_info.state.meta
    assert (meta.minimum, meta.maximum) == (3000, 4000)


def test_rebuild_preserves_current_control_values():
    """A rebuild builds the colour-temp control from scratch, but the surviving control inherits
    its current value instead of the 4000 K default."""
    device = _make_stub_device(lambda: [_setpoint_control(SET_COLOUR_TEMPERATURE, "4000")])
    device.is_initialized = True
    device.rebuild_mqtt_controls()  # first build: default 4000 K

    device.get_mqtt_control(SET_COLOUR_TEMPERATURE).control_info.state.value = "3000"

    device.rebuild_mqtt_controls()  # the TC-limits change would trigger this

    assert device.get_mqtt_control(SET_COLOUR_TEMPERATURE).control_info.state.value == "3000"


def test_rebuild_preserves_error_state():
    """A surviving control keeps its error across a rebuild, symmetric with value — no false OK
    on a genuinely unreadable control."""
    device = _make_stub_device(lambda: [_readable_control("illuminance", "0")])
    device.is_initialized = True
    device.rebuild_mqtt_controls()
    device.get_mqtt_control("illuminance").control_info.state.error = ControlError.READ

    device.rebuild_mqtt_controls()

    assert device.get_mqtt_control("illuminance").control_info.state.error == ControlError.READ


def test_rebuild_preserves_alarm_title_with_its_value():
    """An alarm's title travels with its value across a rebuild, instead of reverting to the
    builder's "Ok" while the value still says the lamp is dead."""
    device = _make_stub_device(lambda: [_alarm_control("alarm")])
    device.is_initialized = True
    device.rebuild_mqtt_controls()
    control = device.get_mqtt_control("alarm")
    control.control_info.state.value = "1"
    control.control_info.state.meta.title = _ALARM_FAULT_TITLE

    device.rebuild_mqtt_controls()

    rebuilt = device.get_mqtt_control("alarm")
    assert rebuilt.control_info.state.value == "1"
    assert rebuilt.control_info.state.meta.title == _ALARM_FAULT_TITLE


def test_rebuild_drops_a_value_the_new_range_no_longer_accepts():
    """The rebuild is what moved the range, so a current value now outside it is replaced by the
    builder default, which the new range accepts."""
    limits = {"minimum": 2000, "maximum": 6500}
    device = _make_stub_device(lambda: [_setpoint_control(SET_COLOUR_TEMPERATURE, "4000", **limits)])
    device.is_initialized = True
    device.rebuild_mqtt_controls()
    device.get_mqtt_control(SET_COLOUR_TEMPERATURE).control_info.state.value = "2700"

    limits.update(minimum=3000, maximum=4000)
    device.rebuild_mqtt_controls()

    assert device.get_mqtt_control(SET_COLOUR_TEMPERATURE).control_info.state.value == "4000"


def test_rebuild_leaves_the_rebuilt_control_due_for_a_poll():
    """The poll schedule is the one thing a rebuild does not carry: the rebuilt control is due
    again, so the settings change that triggered the rebuild is followed by a fresh read."""
    device = _make_stub_device(lambda: [_readable_control("illuminance", "5")])
    device.is_initialized = True
    device.rebuild_mqtt_controls()
    device.get_mqtt_control("illuminance").schedule_next_periodic_poll(polled_at=default_timer())
    assert not device.get_mqtt_control("illuminance").is_poll_due(default_timer())

    device.rebuild_mqtt_controls()

    assert device.get_mqtt_control("illuminance").is_poll_due(default_timer())


def test_rebuild_new_control_uses_builder_default():
    """A control that appears only in the new control set keeps its builder default — there is
    no prior value to inherit."""
    controls = [_setpoint_control(SET_COLOUR_TEMPERATURE, "4000")]
    device = _make_stub_device(lambda: list(controls))
    device.is_initialized = True
    device.rebuild_mqtt_controls()

    controls.append(_readable_control("illuminance", "0", default="7"))
    device.rebuild_mqtt_controls()

    assert device.get_mqtt_control("illuminance").control_info.state.value == "7"


def test_rebuild_dropped_control_disappears():
    """A control absent from the new control set is dropped by the rebuild."""
    controls = [_setpoint_control(SET_COLOUR_TEMPERATURE, "4000"), _readable_control("illuminance", "0")]
    device = _make_stub_device(lambda: list(controls))
    device.is_initialized = True
    device.rebuild_mqtt_controls()

    controls.pop()  # drop illuminance
    device.rebuild_mqtt_controls()

    assert device.get_mqtt_control("illuminance") is None
    assert device.get_mqtt_control(SET_COLOUR_TEMPERATURE) is not None


def test_rebuild_does_not_carry_a_button_press():
    """A pushbutton's value is an event, not state: after a press the rebuilt control starts from
    its placeholder, so the republish reports no press."""
    device = _make_stub_device(lambda: [_pushbutton_control("off")])
    device.is_initialized = True
    device.rebuild_mqtt_controls()
    device.get_mqtt_control("off").control_info.state.value = "1"  # the press that was executed

    device.rebuild_mqtt_controls()

    assert device.get_mqtt_control("off").control_info.state.value is None


# ---------------------------------------------------------------------------
# Scenario 3 — group seeds state from members
# ---------------------------------------------------------------------------


def _make_member(  # pylint: disable=too-many-arguments, R0917
    uid,
    mqtt_id,
    short,
    level_value,
    read_error=False,
    groups=(1,),
    dimming_curve_type=DimmingCurveType.LOGARITHMIC,
    wanted_level_value=None,
):
    """A DaliDevice-like member whose own control objects are what the group seeding observes."""
    control = ActualLevelControl(DimmingCurveState())
    control.control_info.state.value = level_value
    if read_error:
        control.control_info.state.error = ControlError.READ
    controls = {ACTUAL_LEVEL: control}
    if wanted_level_value is not None:
        setpoint = WantedLevelControl(DimmingCurveState())
        setpoint.control_info.state.value = wanted_level_value
        controls[WANTED_LEVEL] = setpoint

    member = MagicMock()
    member.is_initialized = True
    member.uid = uid
    member.mqtt_id = mqtt_id
    member.groups = set(groups)
    member.dt8_colour_type = None
    member.dt8_tc_limits = None
    member.dimming_curve_type = dimming_curve_type
    member.address = MagicMock()
    member.address.short = short
    member.get_group_state_controls = MagicMock(return_value=[control])
    member.get_mqtt_control = MagicMock(side_effect=controls.get)
    return member


def _make_group_controller(dali_devices):
    """Bare controller whose publisher records the group's single publish."""
    return make_group_controller(dali_devices=dali_devices, publisher=_make_publisher())


@pytest.mark.asyncio
async def test_group_publishes_member_current_state_on_create():
    """On creation a group's actual_level control is seeded from a member that has a value, so
    the group is published with that value rather than the empty clone of the first member."""
    # pylint: disable=protected-access
    valueless_member = _make_member(uid="uid-1", mqtt_id="d1", short=1, level_value=None)
    known_member = _make_member(uid="uid-2", mqtt_id="d2", short=2, level_value="50.000")
    ctrl = _make_group_controller([valueless_member, known_member])

    await ctrl._refresh_group_virtual_devices()

    group = ctrl._group_devices_by_number[1]
    assert group.get_mqtt_control(ACTUAL_LEVEL).control_info.state.value == "50.000"
    assert _published_controls(ctrl._device_publisher)[ACTUAL_LEVEL] == "50.000"
    assert _published_errors(ctrl._device_publisher)[ACTUAL_LEVEL] == ControlError.NONE
    error_targets = [call.args[1] for call in ctrl._device_publisher.set_control_error.await_args_list]
    assert ACTUAL_LEVEL not in error_targets


@pytest.mark.asyncio
async def test_group_state_error_when_member_state_unknown():
    """With no candidate holding a value, the group's state control carries ControlError.READ in
    the single add_device publish, not a post-publish set_control_error."""
    # pylint: disable=protected-access
    member = _make_member(uid="uid-1", mqtt_id="d1", short=1, level_value=None)
    ctrl = _make_group_controller([member])

    await ctrl._refresh_group_virtual_devices()

    assert _published_errors(ctrl._device_publisher)[ACTUAL_LEVEL] == ControlError.READ
    ctrl._device_publisher.set_control_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_prefers_a_known_candidate_over_an_errored_one():
    """With one errored and one known candidate the group seeds from the known member and
    publishes without an error, clearing the /meta/error=r inherited from the clone."""
    # pylint: disable=protected-access
    errored_member = _make_member(uid="uid-1", mqtt_id="d1", short=1, level_value="0", read_error=True)
    known_member = _make_member(uid="uid-2", mqtt_id="d2", short=2, level_value="42.000")
    ctrl = _make_group_controller([errored_member, known_member])

    await ctrl._refresh_group_virtual_devices()

    group = ctrl._group_devices_by_number[1]
    assert group.get_mqtt_control(ACTUAL_LEVEL).control_info.state.value == "42.000"
    assert _published_controls(ctrl._device_publisher)[ACTUAL_LEVEL] == "42.000"
    assert _published_errors(ctrl._device_publisher)[ACTUAL_LEVEL] == ControlError.NONE
    error_targets = [call.args[1] for call in ctrl._device_publisher.set_control_error.await_args_list]
    assert ACTUAL_LEVEL not in error_targets


@pytest.mark.asyncio
async def test_group_rebuild_preserves_current_state():
    """A group rebuild (here forced by a member's dimming-curve change) re-seeds its state
    controls from the members' current values, so actual_level is not reset to the default."""
    # pylint: disable=protected-access
    valueless_member = _make_member(uid="uid-1", mqtt_id="d1", short=1, level_value=None)
    known_member = _make_member(uid="uid-2", mqtt_id="d2", short=2, level_value="50.000")
    ctrl = _make_group_controller([valueless_member, known_member])
    await ctrl._refresh_group_virtual_devices()
    assert (
        ctrl._group_devices_by_number[1].get_mqtt_control(ACTUAL_LEVEL).control_info.state.value == "50.000"
    )

    # Force a rebuild: a capability change (dimming curve) replaces the group device.
    valueless_member.dimming_curve_type = DimmingCurveType.LINEAR
    known_member.dimming_curve_type = DimmingCurveType.LINEAR
    await ctrl._refresh_group_virtual_devices()

    rebuilt = ctrl._group_devices_by_number[1]
    assert rebuilt.capabilities.dimming_curve_type == DimmingCurveType.LINEAR
    assert rebuilt.get_mqtt_control(ACTUAL_LEVEL).control_info.state.value == "50.000"


@pytest.mark.asyncio
async def test_group_setpoints_seeded_from_the_state_source_member():
    """The group's wanted_level is seeded from the same member its actual_level came from, so the
    card is published without the setpoint's builder default."""
    # pylint: disable=protected-access
    valueless_member = _make_member(
        uid="uid-1", mqtt_id="d1", short=1, level_value=None, wanted_level_value="7"
    )
    known_member = _make_member(
        uid="uid-2", mqtt_id="d2", short=2, level_value="50.000", wanted_level_value="50"
    )
    ctrl = _make_group_controller([valueless_member, known_member])

    await ctrl._refresh_group_virtual_devices()

    group = ctrl._group_devices_by_number[1]
    assert group.get_mqtt_control(WANTED_LEVEL).control_info.state.value == "50"
    assert _published_controls(ctrl._device_publisher)[WANTED_LEVEL] == "50"


@pytest.mark.asyncio
async def test_group_setpoint_without_a_member_value_keeps_default():
    """A setpoint the seeding member does not have stays at its builder default, with no error of
    its own."""
    # pylint: disable=protected-access
    member = _make_member(uid="uid-1", mqtt_id="d1", short=1, level_value="50.000")
    ctrl = _make_group_controller([member])

    await ctrl._refresh_group_virtual_devices()

    setpoint = ctrl._group_devices_by_number[1].get_mqtt_control(WANTED_LEVEL)
    assert setpoint.control_info.state.value == "0"
    assert setpoint.control_info.state.error == ControlError.NONE
    assert _published_errors(ctrl._device_publisher)[WANTED_LEVEL] == ControlError.NONE
