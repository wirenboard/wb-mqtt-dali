"""EventSyncCoordinator tests: own/foreign commands -> optimistic MQTT
state + confirmation polls, fade tracking, group/broadcast optimism, the aggregate
group topic, and the READY-reentry reconfirm.

Devices are real ``DaliDevice``s carrying only the controls a test needs, installed through
the ``_build_mqtt_controls`` hook. Real controls/handlers are used so prediction logic is
exercised; the DT8 colour handler is initialized through its public ``read_mandatory_info``
with a fake driver (no private attribute access). A fake clock drives settle timing.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import DeviceShort, GearBroadcast, GearGroup, GearShort
from dali.device import occupancy
from dali.frame import BackwardFrame
from dali.gear.colour import (
    Activate,
    SetTemporaryColourTemperature,
    SetTemporaryPrimaryNDimLevel,
    SetTemporaryRGBDimLevel,
    SetTemporaryXCoordinate,
    SetTemporaryYCoordinate,
    XCoordinateStepUp,
    tc_kelvin_mirek,
)
from dali.gear.general import (
    DAPC,
    DTR0,
    DTR1,
    DTR2,
    GoToScene,
    Off,
    QueryStatusResponse,
    SetFadeTime,
)

from wb.mqtt_dali.colour_sequence_tracker import ColourSequenceTracker
from wb.mqtt_dali.common_dali_device import DaliDeviceAddress, DaliDeviceBase
from wb.mqtt_dali.dali2_device import Dali2Device
from wb.mqtt_dali.dali_controls import (
    ActualLevelControl,
    ErrorStatusControl,
    WantedLevelControl,
    make_controls,
)
from wb.mqtt_dali.dali_device import DaliDevice
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.dali_type7_parameters import LastActedControl
from wb.mqtt_dali.dali_type8_common import ColourComponent
from wb.mqtt_dali.dali_type8_parameters import (
    ColourSettings,
    ColourType,
    Type8Parameters,
)
from wb.mqtt_dali.dali_type8_primary_n import (
    get_mqtt_controls as primary_n_mqtt_controls,
)
from wb.mqtt_dali.dali_type8_rgbwaf import get_mqtt_controls as rgbwaf_mqtt_controls
from wb.mqtt_dali.dali_type8_tc import Type8TcLimits
from wb.mqtt_dali.dali_type8_tc import get_mqtt_controls as tc_mqtt_controls
from wb.mqtt_dali.dali_type8_xy import get_mqtt_controls as xy_mqtt_controls
from wb.mqtt_dali.device_publisher import DeviceInfo, DevicePublisher
from wb.mqtt_dali.device_registry import DeviceRegistry
from wb.mqtt_dali.dtr_snapshot import DtrSnapshot
from wb.mqtt_dali.event_sync_coordinator import EventSyncCoordinator
from wb.mqtt_dali.events import (
    ColourChanged,
    Dali2InputEvent,
    EventSource,
    LevelChanged,
    StatusRead,
    SwitchStatusRead,
)
from wb.mqtt_dali.settle_clock import SettleBasis, SettleClock
from wb.mqtt_dali.virtual_devices import (
    _SETPOINT_STATE,
    GroupStateSource,
    GroupVirtualDevice,
)
from wb.mqtt_dali.wbdali_utils import MASK_2BYTES
from wb.mqtt_dali.wbmqtt import ControlError

NOW = 1000.0
RESYNC_INTERVAL = 300.0

# Avoid filesystem reads in DaliDeviceBase.__init__.
# pylint: disable-next=protected-access
DaliDeviceBase._common_schema = {"title": "test-schema"}


class _SceneStub:  # pylint: disable=too-few-public-methods
    def __init__(self, levels: dict) -> None:
        self._levels = levels

    def scene_level(self, index: int):
        return self._levels.get(index)


class _TestGearDevice(DaliDevice):
    """Gear device carrying exactly the controls a test hands it, plus the colour controls its
    DT8 handler declares. A real ``DaliDevice`` and not a stand-in: dispatch goes through
    ``notify_all``, and the group aggregate is mirrored for real gear only.
    """

    def __init__(self, short: int, controls, groups=(), dt8_handler=None) -> None:
        super().__init__(DaliDeviceAddress(short=short, random=0), "bus", MagicMock())
        self.mqtt_id = f"dev-{short}"
        self.name = f"dev{short}"
        self._test_controls = list(controls)
        self._test_groups = set(groups)
        # The real field, so dt8_colour_type and dt8_tc_limits report this handler too.
        self._type8_handler = dt8_handler
        self.rebuild_mqtt_controls()

    @property
    def groups(self) -> set[int]:
        return self._test_groups

    @property
    def dt8_handler(self):
        return self._type8_handler

    @dt8_handler.setter
    def dt8_handler(self, handler) -> None:
        self._type8_handler = handler
        self.rebuild_mqtt_controls()

    def _build_mqtt_controls(self):
        colour = [] if self._type8_handler is None else self._type8_handler.get_mqtt_controls()
        return [*self._test_controls, *colour]


def _linear_curve() -> DimmingCurveState:
    curve = DimmingCurveState()
    curve.curve_type = DimmingCurveType.LINEAR
    return curve


class _GearDevice(_TestGearDevice):
    """Gear device whose only level representation is ``actual_level`` (plus a Type-7
    ``last_acted`` when the test brings one)."""

    def __init__(  # pylint: disable=too-many-arguments,R0917
        self,
        short: int,
        groups=(),
        max_level=None,
        min_level=None,
        scene_source=None,
        fade_code=None,
        last_acted=None,
    ) -> None:
        controls = [
            ActualLevelControl(
                _linear_curve(),
                max_level=SimpleNamespace(value=max_level),
                min_level=SimpleNamespace(value=min_level),
                scene_source=scene_source if scene_source is not None else _SceneStub({}),
            )
        ]
        if last_acted is not None:
            controls.append(last_acted)
        super().__init__(short, controls, groups=groups)
        if fade_code is not None:
            self.fade_param.set_fade_time(fade_code)


def _fmt(raw: int) -> str:
    return f"{_linear_curve().get_level(raw):.3f}"


def _coordinator(devices, group_devices=None):
    registry = DeviceRegistry()
    registry.set_gear_devices(devices)
    publisher = AsyncMock()
    coordinator = EventSyncCoordinator(
        publisher=publisher,
        device_registry=registry,
        group_devices_by_number=group_devices if group_devices is not None else {},
        logger=MagicMock(),
        settle_clock=SettleClock(),
        now_fn=lambda: NOW,
    )
    return coordinator, publisher


def _prime_poll(control) -> None:
    """Polled at NOW, next poll one exact base interval later — no startup reconfirm, no jitter,
    so a confirmation has something unambiguous to move."""
    control.next_due_at = NOW + RESYNC_INTERVAL


async def _make_colour_handler(colour_type: ColourType) -> Type8Parameters:
    handler = Type8Parameters()
    driver = AsyncMock()
    status = MagicMock()
    status.raw_value = MagicMock(error=False)
    status.colour_type_xy_active = colour_type == ColourType.XY
    status.colour_type_colour_temperature_Tc_active = colour_type == ColourType.COLOUR_TEMPERATURE
    status.colour_type_primary_N_active = colour_type == ColourType.PRIMARY_N
    driver.send = AsyncMock(return_value=status)
    limit = MagicMock()
    limit.raw_value = None
    driver.send_commands = AsyncMock(return_value=[limit for _ in range(13)])
    await handler.read_mandatory_info(driver, GearShort(5))
    return handler


def _group_with_member(member) -> GroupVirtualDevice:
    """The group virtual device of group 2, composed from ``member`` as its only candidate."""
    member.is_initialized = True
    registry = DeviceRegistry()
    registry.set_gear_devices([member])
    return GroupVirtualDevice(2, registry, "bus", "Bus")


def _published(publisher) -> dict:
    """Map (device_id, control_id) -> last published value, over both publish paths: a
    control's own state and the group aggregate mirrored from it."""
    calls = [
        *publisher.publish_control_state.await_args_list,
        *publisher.set_control_value.await_args_list,
    ]
    return {(c.args[0], c.args[1]): c.args[2] for c in calls}


def _nothing_published(publisher) -> None:
    publisher.publish_control_state.assert_not_awaited()
    publisher.set_control_value.assert_not_awaited()


# --- Optimistic level updates -------------------------------------------


@pytest.mark.asyncio
async def test_predictable_command_publishes_formatted_level():
    device = _GearDevice(5)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])

    assert _published(publisher)[("dev-5", "actual_level")] == _fmt(200)


@pytest.mark.asyncio
async def test_unknown_device_command_ignored():
    """A command to a short address with no device: no publish, no exception."""
    device = _GearDevice(5)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(9), 100)])

    _nothing_published(publisher)


# --- Confirmation poll & fade -------------------------------------------


@pytest.mark.asyncio
async def test_external_command_schedules_confirmation_poll():
    """A predictable command pulls the affected control's next poll to ~fade settle
    (a single read), not the long re-sync interval."""
    device = _GearDevice(5, fade_code=8)  # 8.0s fade
    actual = device.get_mqtt_control("actual_level")
    _prime_poll(actual)
    coordinator, _ = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 100)])

    expected = SettleClock().settle_for(SettleBasis.FADE, 8)
    assert actual.next_due_at == pytest.approx(NOW + expected)
    assert actual.next_due_at < NOW + RESYNC_INTERVAL


@pytest.mark.asyncio
async def test_fade_time_unknown_uses_default_delay():
    """A fading command on a device with no known fade code schedules its confirmation
    poll at the SettleClock default-delay, not a fade-derived time."""
    device = _GearDevice(5, fade_code=None)
    actual = device.get_mqtt_control("actual_level")
    _prime_poll(actual)
    coordinator, _ = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 100)])

    assert actual.next_due_at == pytest.approx(NOW + SettleClock().settle_for(SettleBasis.FADE, None))


@pytest.mark.asyncio
async def test_external_set_fade_time_then_dapc_uses_new_fade():
    """A sniffed DTR0+SetFadeTime updates the device fade; the following DAPC's confirm
    poll is scheduled by the new fade, not the init value."""
    device = _GearDevice(5, fade_code=2)  # init fade 1.0s
    actual = device.get_mqtt_control("actual_level")
    _prime_poll(actual)
    coordinator, _ = _coordinator([device])

    await coordinator.apply_commands([DTR0(10), SetFadeTime(GearShort(5)), DAPC(GearShort(5), 120)])

    assert device.fade_param.fade_time == 10
    assert actual.next_due_at == pytest.approx(NOW + SettleClock().settle_for(SettleBasis.FADE, 10))


@pytest.mark.asyncio
async def test_confirmation_poll_is_single_read():
    """After a confirm poll is scheduled and the read happens, the interval re-draws to
    the long base — no early re-read is queued."""
    device = _GearDevice(5, fade_code=2)
    actual = device.get_mqtt_control("actual_level")
    _prime_poll(actual)
    coordinator, _ = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 100)])
    assert actual.next_due_at < NOW + RESYNC_INTERVAL  # pulled in for the confirm

    # The read fires; the control re-draws its interval back to the long base.
    actual.next_poll_step(None, GearShort(5), max_commands=3, default_max_commands=3, now=NOW + 2.0)
    assert actual.next_due_at >= NOW + 2.0 + RESYNC_INTERVAL * 0.7


# --- Type 7 last_acted ---------------------------------------------------


@pytest.mark.asyncio
async def test_type7_last_acted_predicted_from_level_crossing():
    """A level command to a Type-7 device whose switch thresholds are known predicts
    last_acted from the crossing (50 -> 200 crosses up-on=150 -> code "1") and schedules
    a confirmation poll alongside actual_level."""
    last_acted = LastActedControl(
        up_on=SimpleNamespace(value=150),
        up_off=SimpleNamespace(value=80),
        down_on=SimpleNamespace(value=40),
        down_off=SimpleNamespace(value=20),
    )
    device = _GearDevice(5, max_level=254, last_acted=last_acted)
    actual = device.get_mqtt_control("actual_level")
    device.notify_all(LevelChanged(50, EventSource.READ))  # prime level 50 on both controls
    _prime_poll(actual)
    _prime_poll(last_acted)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])  # 50 -> 200 crosses up-on (150)

    assert _published(publisher)[("dev-5", "last_acted")] == "1"
    assert last_acted.next_due_at < NOW + RESYNC_INTERVAL  # also confirm-polled


# --- Confirmation deadlines shared by the level readers ------------------


@pytest.mark.asyncio
async def test_observed_event_schedules_one_shared_confirmation():
    """A Type-7 device whose level two controls read: a confirmed read schedules nothing, and
    the sniffed command that follows leaves both the very same settle deadline."""
    last_acted = LastActedControl(
        up_on=SimpleNamespace(value=150),
        up_off=SimpleNamespace(value=80),
        down_on=SimpleNamespace(value=40),
        down_off=SimpleNamespace(value=20),
    )
    device = _GearDevice(5, max_level=254, last_acted=last_acted, fade_code=8)  # 8.0s fade
    actual = device.get_mqtt_control("actual_level")
    _prime_poll(actual)
    _prime_poll(last_acted)
    coordinator, _ = _coordinator([device])

    await coordinator.notify_poll_event(device, LevelChanged(100, EventSource.READ))

    assert actual.next_due_at == NOW + RESYNC_INTERVAL
    assert last_acted.next_due_at == NOW + RESYNC_INTERVAL

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])  # 100 -> 200 crosses up-on (150)

    expected = NOW + SettleClock().settle_for(SettleBasis.FADE, 8)
    assert actual.next_due_at == pytest.approx(expected)
    assert last_acted.next_due_at == pytest.approx(expected)


@pytest.mark.asyncio
async def test_confirmation_scheduled_even_when_value_refused():
    """Every control that can refuse a sniffed value, refusing one: actual_level and
    current_rgb under a standing read error, last_acted because 200 -> 210 crosses no
    threshold. Nothing is published, and every confirming poll is scheduled all the same.
    """
    last_acted = LastActedControl(
        up_on=SimpleNamespace(value=100),  # already crossed at the primed level 200
        up_off=SimpleNamespace(value=50),
        down_on=SimpleNamespace(value=40),
        down_off=SimpleNamespace(value=20),
    )
    device = _GearDevice(5, max_level=254, last_acted=last_acted, fade_code=8)
    actual = device.get_mqtt_control("actual_level")
    device.notify_all(LevelChanged(200, EventSource.READ))  # the level both compare against
    actual.control_info.state.error = ControlError.READ  # its own poll is failing
    _prime_poll(actual)
    _prime_poll(last_acted)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 210)])  # crosses nothing on the way up

    _nothing_published(publisher)
    expected = NOW + SettleClock().settle_for(SettleBasis.FADE, 8)
    assert actual.next_due_at == pytest.approx(expected)
    assert last_acted.next_due_at == pytest.approx(expected)
    assert actual.control_info.state.error == ControlError.READ

    handler = await _make_colour_handler(ColourType.RGBWAF)
    colour_device = _TestGearDevice(6, [], dt8_handler=handler)
    colour_device.get_mqtt_control("current_rgb").control_info.state.error = ControlError.READ
    _prime_poll(handler)
    colour_coordinator, colour_publisher = _coordinator([colour_device])

    await colour_coordinator.apply_commands(
        [DTR0(10), DTR1(20), DTR2(30), SetTemporaryRGBDimLevel(GearShort(6)), Activate(GearShort(6))]
    )

    assert ("dev-6", "current_rgb") not in _published(colour_publisher)
    # A colour control schedules nothing itself; the handler is re-polled regardless.
    assert handler.next_due_at == pytest.approx(NOW + SettleClock().settle_for(SettleBasis.FADE, None))


@pytest.mark.asyncio
async def test_last_acted_refuses_the_crossing_under_its_own_read_error():
    """A sniffed crossing under last_acted's own standing /meta/error=r: the fresh code is not
    put on the wire beside it, the confirming poll is scheduled all the same, and the crossing
    base still moves to the level the refused event carried."""
    last_acted = LastActedControl(
        up_on=SimpleNamespace(value=150),
        up_off=SimpleNamespace(value=80),
        down_on=SimpleNamespace(value=40),
        down_off=SimpleNamespace(value=20),
    )
    device = _GearDevice(5, max_level=254, last_acted=last_acted, fade_code=8)
    device.notify_all(LevelChanged(50, EventSource.READ))  # the level the crossing starts from
    device.notify_all(SwitchStatusRead(None, True))  # its own poll failed
    _prime_poll(last_acted)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])  # 50 -> 200 crosses up-on (150)

    assert ("dev-5", "last_acted") not in _published(publisher)
    assert last_acted.control_info.state.value == "0"  # the declared default, untouched
    assert last_acted.control_info.state.error == ControlError.READ
    assert last_acted.next_due_at == pytest.approx(NOW + SettleClock().settle_for(SettleBasis.FADE, 8))

    last_acted.control_info.state.error = ControlError.NONE  # its next read answered

    await coordinator.apply_commands([DAPC(GearShort(5), 50)])  # 200 -> 50 crosses up-off (80)

    assert _published(publisher)[("dev-5", "last_acted")] == "2"


# --- Group / broadcast ---------------------------------------------------


@pytest.mark.asyncio
async def test_group_dapc_optimistic_for_all_members():
    members = [_GearDevice(s, groups={2}) for s in (3, 4)]
    coordinator, publisher = _coordinator(members)

    await coordinator.apply_commands([DAPC(GearGroup(2), 200)])

    published = _published(publisher)
    assert published[("dev-3", "actual_level")] == _fmt(200)
    assert published[("dev-4", "actual_level")] == _fmt(200)


@pytest.mark.asyncio
async def test_group_goto_scene_optimistic_only_for_cached_members():
    cached = _GearDevice(3, groups={2}, scene_source=_SceneStub({5: 130}))
    uncached = _GearDevice(4, groups={2}, scene_source=_SceneStub({}))
    coordinator, publisher = _coordinator([cached, uncached])

    await coordinator.apply_commands([GoToScene(GearGroup(2), 5)])

    published = _published(publisher)
    assert published[("dev-3", "actual_level")] == _fmt(130)
    assert ("dev-4", "actual_level") not in published  # scene unknown -> poll only


@pytest.mark.asyncio
async def test_broadcast_command_updates_all_devices():
    devices = [_GearDevice(s) for s in (1, 2, 3)]
    coordinator, publisher = _coordinator(devices)

    await coordinator.apply_commands([DAPC(GearBroadcast(), 254)])

    published = _published(publisher)
    for short in (1, 2, 3):
        assert published[(f"dev-{short}", "actual_level")] == _fmt(254)


@pytest.mark.asyncio
async def test_group_aggregate_mirrors_optimistic_value():
    """A predictable group command also moves the group's aggregate topic (pinned member)."""
    member = _GearDevice(3, groups={2})
    group_device = _group_with_member(member)
    coordinator, publisher = _coordinator([member], group_devices={2: group_device})

    await coordinator.apply_commands([DAPC(GearGroup(2), 200)])

    assert _published(publisher)[(group_device.mqtt_id, "actual_level")] == _fmt(200)


@pytest.mark.asyncio
async def test_group_aggregate_untouched_for_unpredictable_member():
    """When the pinned member's value is unpredictable, the group topic is left for its poll."""
    member = _GearDevice(3, groups={2}, scene_source=_SceneStub({}))  # scene unknown
    group_device = SimpleNamespace(
        mqtt_id="group-2",
        state_source=GroupStateSource({"actual_level": [member.uid]}),
    )
    coordinator, publisher = _coordinator([member], group_devices={2: group_device})

    await coordinator.apply_commands([GoToScene(GearGroup(2), 7)])

    assert ("group-2", "actual_level") not in _published(publisher)


# --- DT8 colour ----------------------------------------------------------


@pytest.mark.asyncio
async def test_external_xy_color_command_updates_topic():
    """A foreign XY colour command (DTR0/1 + SetTemporaryXCoordinate + Activate)
    optimistically publishes the x-coordinate topic from the captured DTR word."""
    handler = await _make_colour_handler(ColourType.XY)
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    coordinator, publisher = _coordinator([device])

    x_word = 0x1234
    await coordinator.apply_commands(
        [
            DTR0(x_word & 0xFF),
            DTR1((x_word >> 8) & 0xFF),
            SetTemporaryXCoordinate(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )

    assert _published(publisher)[("dev-5", "current_x_coordinate")] == str(x_word)


@pytest.mark.asyncio
async def test_external_color_temperature_command_updates_topic():
    """A foreign Tc colour command (DTR0/1 + SetTemporaryColourTemperature + Activate)
    within the device's Tc limits optimistically publishes the colour-temperature topic
    (in Kelvin) from the captured mirek word."""
    handler = await _make_colour_handler(ColourType.COLOUR_TEMPERATURE)
    handler.tc_limits.tc_min_mirek = 100
    handler.tc_limits.tc_max_mirek = 500
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    coordinator, publisher = _coordinator([device])

    tc = 250  # mirek, within limits
    await coordinator.apply_commands(
        [
            DTR0(tc & 0xFF),
            DTR1((tc >> 8) & 0xFF),
            SetTemporaryColourTemperature(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )

    assert _published(publisher)[("dev-5", "current_colour_temperature")] == str(tc_kelvin_mirek(tc))


@pytest.mark.asyncio
async def test_color_temperature_clamped_to_tc_limits():
    """A foreign Tc command below the device's coolest limit clamps up to tc_min before
    the optimistic colour-temperature topic is published."""
    handler = await _make_colour_handler(ColourType.COLOUR_TEMPERATURE)
    handler.tc_limits.tc_min_mirek = 200
    handler.tc_limits.tc_max_mirek = 400
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    coordinator, publisher = _coordinator([device])

    tc = 50  # below the coolest limit -> clamps up to 200
    await coordinator.apply_commands(
        [
            DTR0(tc & 0xFF),
            DTR1((tc >> 8) & 0xFF),
            SetTemporaryColourTemperature(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )

    assert _published(publisher)[("dev-5", "current_colour_temperature")] == str(tc_kelvin_mirek(200))


@pytest.mark.asyncio
async def test_external_rgbwaf_color_command_updates_topic():
    """A foreign RGB command (DTR0/1/2 + SetTemporaryRGBDimLevel + Activate) on an RGBWAF
    device optimistically publishes current_rgb; the W/A/F components it did not set are
    left at the MASK sentinel and are not published as real values."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands(
        [
            DTR0(10),
            DTR1(20),
            DTR2(30),
            SetTemporaryRGBDimLevel(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )

    published = _published(publisher)
    assert published[("dev-5", "current_rgb")] == "10;20;30"
    assert ("dev-5", "current_white") not in published  # unset (MASK) -> not published


@pytest.mark.asyncio
async def test_external_primary_n_color_command_updates_topic():
    """A foreign primary-N command (DTR2 selects the primary, DTR1:DTR0 its level, then
    SetTemporaryPrimaryNDimLevel + Activate) publishes that primary's current_primary_n
    topic; the other primaries stay at the MASK sentinel and are not published."""
    handler = await _make_colour_handler(ColourType.PRIMARY_N)
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    coordinator, publisher = _coordinator([device])

    level = 0x0102  # DTR1:DTR0
    await coordinator.apply_commands(
        [
            DTR0(level & 0xFF),
            DTR1((level >> 8) & 0xFF),
            DTR2(2),  # selects primary index 2
            SetTemporaryPrimaryNDimLevel(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )

    published = _published(publisher)
    assert published[("dev-5", "current_primary_n2")] == str(level)
    assert ("dev-5", "current_primary_n0") not in published  # other primaries not published


@pytest.mark.asyncio
async def test_colour_overlay_leaves_untouched_component_alone():
    """A second partial colour command overlays only the component it carries: the first
    command's coordinate is neither lost nor reset to the MASK sentinel, and the picture goes
    out whole, so the untouched coordinate is reported again with the value it already had."""
    handler = await _make_colour_handler(ColourType.XY)
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    coordinator, publisher = _coordinator([device])

    x_word, y_word = 0x1111, 0x2222
    await coordinator.apply_commands(
        [
            DTR0(x_word & 0xFF),
            DTR1(x_word >> 8),
            SetTemporaryXCoordinate(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )
    await coordinator.apply_commands(
        [
            DTR0(y_word & 0xFF),
            DTR1(y_word >> 8),
            SetTemporaryYCoordinate(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )

    x_publishes = [
        c for c in publisher.publish_control_state.await_args_list if c.args[1] == "current_x_coordinate"
    ]
    # Reported by both commands, and by the second one unchanged -- not dropped as MASK either.
    assert [c.args[2] for c in x_publishes] == [str(x_word), str(x_word)]
    published = _published(publisher)
    assert published[("dev-5", "current_x_coordinate")] == str(x_word)
    assert published[("dev-5", "current_y_coordinate")] == str(y_word)


def test_colour_sequence_drops_orphaned_capture_on_new_transaction():
    """A capture left un-taken by an Activate (it addressed a different target, clearing
    _fresh_dtr) is dropped when the next colour transaction's first DTR write begins, so a
    stale/poisoned capture can't bleed into the fresh sequence."""
    tracker = ColourSequenceTracker()
    snapshot = DtrSnapshot()
    tracker.note_dtr(0)
    tracker.note_dtr(1)
    tracker.record("uid-5", SetTemporaryXCoordinate(GearShort(5)), snapshot)
    tracker.end_activate()  # an Activate ran but addressed a different device; uid-5 not taken
    tracker.note_dtr(0)  # a new transaction begins -> orphaned capture is cleared
    assert tracker.take("uid-5") is None


@pytest.mark.asyncio
async def test_partial_color_sequence_polls_without_optimistic_value():
    """SetTemporary without the preceding DTR writes (started mid-stream) -> poll only."""
    handler = await _make_colour_handler(ColourType.XY)
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    _prime_poll(handler)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([SetTemporaryXCoordinate(GearShort(5)), Activate(GearShort(5))])

    _nothing_published(publisher)
    assert handler.next_due_at < NOW + RESYNC_INTERVAL  # still confirm-polled


@pytest.mark.asyncio
async def test_color_step_command_polls_without_optimistic_value():
    """A relative colour-step command (magnitude unknown) publishes nothing optimistically
    but still schedules a confirmation poll of the colour handler."""
    handler = await _make_colour_handler(ColourType.XY)
    device = _GearDevice(5, fade_code=2)
    device.dt8_handler = handler
    _prime_poll(handler)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([XCoordinateStepUp(GearShort(5))])

    _nothing_published(publisher)
    assert handler.next_due_at < NOW + RESYNC_INTERVAL


@pytest.mark.asyncio
async def test_goto_scene_uses_cached_scene_colour():
    """GoToScene on a DT8 device restores the cached scene colour onto the current_* topics."""
    handler = await _make_colour_handler(ColourType.XY)
    scene_colour = ColourSettings(ColourType.XY, level=120)
    scene_colour.colour.x_coordinate = 4242
    scene_colour.colour.y_coordinate = 1111
    driver = AsyncMock()
    driver.run_sequence = AsyncMock(return_value=scene_colour)
    await handler.scenes_settings.read(driver, GearShort(5))  # populate scene colours

    device = _GearDevice(5, fade_code=2, scene_source=handler.scenes_settings)
    device.dt8_handler = handler
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([GoToScene(GearShort(5), 3)])

    published = _published(publisher)
    assert published[("dev-5", "actual_level")] == _fmt(120)
    assert published[("dev-5", "current_x_coordinate")] == "4242"


@pytest.mark.asyncio
async def test_goto_scene_colour_published_without_a_level_control():
    """A scene sets colour as well as level, so a DT8 device carrying no actual_level control
    still gets the cached scene colour on its current_* topics."""
    handler = await _make_colour_handler(ColourType.XY)
    scene_colour = ColourSettings(ColourType.XY, level=120)
    scene_colour.colour.x_coordinate = 4242
    driver = AsyncMock()
    driver.run_sequence = AsyncMock(return_value=scene_colour)
    await handler.scenes_settings.read(driver, GearShort(5))  # populate scene colours

    device = _TestGearDevice(5, [], dt8_handler=handler)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([GoToScene(GearShort(5), 3)])

    assert _published(publisher)[("dev-5", "current_x_coordinate")] == "4242"


# --- Representation mirroring --------------------------------------------


def _level_controls(curve):
    return [
        ActualLevelControl(
            curve,
            max_level=SimpleNamespace(value=254),
            min_level=SimpleNamespace(value=1),
            scene_source=_SceneStub({}),
        ),
        WantedLevelControl(curve),
        *make_controls(),  # includes the dapc control
    ]


def _level_device(short=5) -> _TestGearDevice:
    """Gear device carrying the full set of the level quantity's representation controls."""
    return _TestGearDevice(short, _level_controls(_linear_curve()))


def _colour_device(handler, short=5, groups=()) -> _TestGearDevice:
    """Gear device carrying the full set of representation controls: the level ones plus the
    colour controls ``handler`` declares."""
    return _TestGearDevice(short, _level_controls(_linear_curve()), groups=groups, dt8_handler=handler)


def _rgb_read(red: int, green: int, blue: int) -> ColourChanged:
    return ColourChanged(
        {
            ColourComponent.RED: red,
            ColourComponent.GREEN: green,
            ColourComponent.BLUE: blue,
        },
        EventSource.READ,
    )


@pytest.mark.asyncio
async def test_level_change_mirrors_representations():
    """A level command publishes all three level representations from the observed raw:
    actual_level as the fractional %, wanted_level as that % rounded to an integer (it is an
    integer-only control), dapc as the raw value. Raw 200 -> 78.740% makes the rounding visible."""
    device = _level_device()
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])

    published = _published(publisher)
    assert published[("dev-5", "actual_level")] == _fmt(200)  # "78.740"
    assert published[("dev-5", "wanted_level")] == "79"  # round(78.740)
    assert published[("dev-5", "dapc")] == "200"


@pytest.mark.asyncio
async def test_rgb_change_mirrors_representations():
    """An RGB colour command publishes both the state (current_rgb) and setpoint (set_rgb)
    representations from the same observed colour."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _colour_device(handler)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands(
        [DTR0(10), DTR1(20), DTR2(30), SetTemporaryRGBDimLevel(GearShort(5)), Activate(GearShort(5))]
    )

    published = _published(publisher)
    assert published[("dev-5", "current_rgb")] == "10;20;30"
    assert published[("dev-5", "set_rgb")] == "10;20;30"


@pytest.mark.asyncio
async def test_tc_change_mirrors_representations():
    """A colour-temperature command publishes both current_colour_temperature and
    set_colour_temperature (Kelvin) from the observed mirek."""
    handler = await _make_colour_handler(ColourType.COLOUR_TEMPERATURE)
    handler.tc_limits.tc_min_mirek = 100
    handler.tc_limits.tc_max_mirek = 500
    device = _colour_device(handler)
    coordinator, publisher = _coordinator([device])

    tc = 250  # mirek, within limits
    await coordinator.apply_commands(
        [
            DTR0(tc & 0xFF),
            DTR1((tc >> 8) & 0xFF),
            SetTemporaryColourTemperature(GearShort(5)),
            Activate(GearShort(5)),
        ]
    )

    published = _published(publisher)
    assert published[("dev-5", "current_colour_temperature")] == str(tc_kelvin_mirek(tc))
    assert published[("dev-5", "set_colour_temperature")] == str(tc_kelvin_mirek(tc))


@pytest.mark.asyncio
async def test_setpoint_write_mirrors_representations():
    """Writing a setpoint emits the same command a foreign change would; applying it
    republishes every representation of the quantity, not just the setpoint written."""
    device = _level_device()
    coordinator, publisher = _coordinator([device])

    wanted = device.get_mqtt_control("wanted_level")
    (dapc_cmd,) = wanted.get_setup_commands(GearShort(5), "60")  # wanted_level=60% -> DAPC(152)
    await coordinator.apply_commands([dapc_cmd])

    published = _published(publisher)
    # raw 152 renders to a fractional 59.843%, so wanted_level rounds back up to 60.
    assert published[("dev-5", "actual_level")] == _fmt(152)
    assert published[("dev-5", "wanted_level")] == "60"
    assert published[("dev-5", "dapc")] == "152"


@pytest.mark.asyncio
async def test_foreign_command_mirrors_setpoints():
    """A foreign level command and a foreign colour command both move their setpoints
    (which used to freeze), not only the state controls."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _colour_device(handler)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 100)])
    await coordinator.apply_commands(
        [DTR0(10), DTR1(20), DTR2(30), SetTemporaryRGBDimLevel(GearShort(5)), Activate(GearShort(5))]
    )

    published = _published(publisher)
    assert published[("dev-5", "wanted_level")] == "39"  # round(_fmt(100) == "39.370")
    assert published[("dev-5", "dapc")] == "100"
    assert published[("dev-5", "set_rgb")] == "10;20;30"


@pytest.mark.asyncio
async def test_poll_readback_mirrors_representations():
    """A re-sync poll's state readbacks mirror onto the quantity's setpoints, so they sync
    even for commands prediction can't follow."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _colour_device(handler)
    coordinator, publisher = _coordinator([device])

    await coordinator.notify_poll_event(device, LevelChanged(180, EventSource.READ))
    await coordinator.notify_poll_event(device, _rgb_read(1, 2, 3))
    await coordinator.notify_poll_event(device, ColourChanged({ColourComponent.WHITE: 4}, EventSource.READ))

    published = _published(publisher)
    assert published[("dev-5", "actual_level")] == _fmt(180)
    assert published[("dev-5", "wanted_level")] == "71"  # round(_fmt(180) == "70.866")
    assert published[("dev-5", "dapc")] == "180"
    assert published[("dev-5", "set_rgb")] == "1;2;3"
    assert published[("dev-5", "set_white")] == "4"


@pytest.mark.asyncio
async def test_poll_readback_skips_failed_reads():
    """A poll batch mixing failed reads with a good one mirrors setpoints only for the good
    one: a read that brought no value leaves a setpoint nothing to show."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _colour_device(handler)
    coordinator, publisher = _coordinator([device])

    await coordinator.notify_poll_event(device, LevelChanged(None, EventSource.READ, failed=True))
    await coordinator.notify_poll_event(
        device, ColourChanged({ColourComponent.WHITE: None}, EventSource.READ, failed=True)
    )
    await coordinator.notify_poll_event(device, _rgb_read(1, 2, 3))

    published = _published(publisher)
    assert ("dev-5", "wanted_level") not in published
    assert ("dev-5", "dapc") not in published
    assert ("dev-5", "set_white") not in published
    assert published[("dev-5", "set_rgb")] == "1;2;3"


@pytest.mark.asyncio
async def test_level_read_failure_marks_actual_level_only():
    """A good level read primes all three representations, then the same read fails: only
    actual_level is published again, with `/meta/error=r` over the value it keeps, while its
    setpoints hold their values error-free."""
    device = _level_device()
    coordinator, publisher = _coordinator([device])

    await coordinator.notify_poll_event(device, LevelChanged(180, EventSource.READ))
    published_before_failure = len(publisher.publish_control_state.await_args_list)

    await coordinator.notify_poll_event(device, LevelChanged(None, EventSource.READ, failed=True))

    failure_calls = publisher.publish_control_state.await_args_list[published_before_failure:]
    assert [(call.args[1], call.args[3]) for call in failure_calls] == [("actual_level", ControlError.READ)]
    states = {
        control_id: device.get_mqtt_control(control_id).control_info.state
        for control_id in ("actual_level", "wanted_level", "dapc")
    }
    assert states["actual_level"].value == _fmt(180)  # the last known level stands under the error
    assert (states["wanted_level"].value, states["wanted_level"].error) == ("71", ControlError.NONE)
    assert (states["dapc"].value, states["dapc"].error) == ("180", ControlError.NONE)


@pytest.mark.asyncio
async def test_meta_title_change_without_value_change_still_publishes():
    """Two failing status reads in a row name different bits, so the second read changes the
    title alone -- and is published all the same, carrying the new title."""
    device = _TestGearDevice(5, [ErrorStatusControl()])
    coordinator, publisher = _coordinator([device])

    for status_bit in ("lamp failure", "ballast status"):
        response = QueryStatusResponse(BackwardFrame(1 << QueryStatusResponse.bits.index(status_bit)))
        await coordinator.notify_poll_event(device, StatusRead(response, False))

    calls = publisher.publish_control_state.await_args_list
    assert [call.args[2] for call in calls] == ["1", "1"]  # the value never moved
    assert [call.args[4].en for call in calls] == ["Lamp failure", "Ballast not ok"]


@pytest.mark.asyncio
async def test_level_suppressed_while_read_error():
    """While the level's read poll is failing, `actual_level` refuses the observed value, so
    nothing republishes it and its standing /meta/error=r is not cleared. Its setpoints have
    no read error to protect and mirror the observed value as usual."""
    device = _level_device()
    device.get_mqtt_control("actual_level").control_info.state.error = ControlError.READ
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])

    published = _published(publisher)
    assert ("dev-5", "actual_level") not in published
    assert published[("dev-5", "dapc")] == "200"


@pytest.mark.asyncio
async def test_colour_suppressed_while_read_error():
    """While a colour state control's read poll is failing, the observed colour does not
    republish it (its /meta/error=r stands); the paired setpoint, having no read error of its
    own, still mirrors."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _colour_device(handler)
    device.get_mqtt_control("current_rgb").control_info.state.error = ControlError.READ
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands(
        [DTR0(10), DTR1(20), DTR2(30), SetTemporaryRGBDimLevel(GearShort(5)), Activate(GearShort(5))]
    )

    published = _published(publisher)
    assert ("dev-5", "current_rgb") not in published
    assert published[("dev-5", "set_rgb")] == "10;20;30"


@pytest.mark.asyncio
async def test_read_error_no_flicker():
    """A stream of commands while a read error stands never publishes the reading control — so
    nothing clears its /meta/error and it never flickers r <-> "" under live traffic."""
    device = _level_device()
    device.get_mqtt_control("actual_level").control_info.state.error = ControlError.READ
    coordinator, publisher = _coordinator([device])

    for _ in range(5):
        await coordinator.apply_commands([DAPC(GearShort(5), 200), Off(GearShort(5))])

    assert ("dev-5", "actual_level") not in _published(publisher)
    publisher.set_control_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_topic_single_publish():
    """A group command moves each group state topic (level and colour) exactly once and
    mirrors the pinned member's owned setpoints (wanted_level/dapc/set_*) once each, with
    the same value — so the group card's setpoints track the member whose state it shows."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    member = _colour_device(handler, short=3, groups={2})
    group_device = _group_with_member(member)
    coordinator, publisher = _coordinator([member], group_devices={2: group_device})

    await coordinator.apply_commands([DAPC(GearGroup(2), 200)])
    await coordinator.apply_commands(
        [DTR0(10), DTR1(20), DTR2(30), SetTemporaryRGBDimLevel(GearGroup(2)), Activate(GearGroup(2))]
    )

    group_values: dict[str, list[str]] = {}
    for call in publisher.publish_control_state.await_args_list:
        if call.args[0] == group_device.mqtt_id:
            group_values.setdefault(call.args[1], []).append(call.args[2])
    published = _published(publisher)
    # Each state topic and each owned setpoint moves exactly once...
    for control_id in ("actual_level", "current_rgb", "wanted_level", "dapc", "set_rgb"):
        assert len(group_values.get(control_id, [])) == 1, control_id
    # ...and the setpoints carry the pinned member's value verbatim.
    for setpoint in ("wanted_level", "dapc", "set_rgb"):
        assert published[(group_device.mqtt_id, setpoint)] == published[("dev-3", setpoint)]


# --- DALI-2 input events ------------------------------------------------


@pytest.mark.asyncio
async def test_dali2_event_reaches_the_topic_of_the_control_that_took_it():
    """The DALI-2 leg of the dispatch, end to end over a real publisher: an occupancy event
    reaches a real Dali2Device and costs exactly one message -- movement0, the only control
    whose value moved (occupied0 took it unchanged, and there is no gear group to mirror)."""
    device = Dali2Device(DaliDeviceAddress(short=7, random=0), "bus", MagicMock())
    device.add_instance(0, occupancy.instance_type)
    device.rebuild_mqtt_controls()
    device.is_initialized = True
    mqtt_dispatcher = MagicMock()
    mqtt_dispatcher.publish = AsyncMock()
    publisher = DevicePublisher(mqtt_dispatcher, logging.getLogger("test.dali2"))
    await publisher.initialize()
    await publisher.add_device(DeviceInfo(id=device.mqtt_id, controls=device.get_mqtt_controls()))
    coordinator = EventSyncCoordinator(
        publisher=publisher,
        device_registry=DeviceRegistry(),
        group_devices_by_number={1: SimpleNamespace(mqtt_id="group-1")},
        logger=MagicMock(),
        settle_clock=SettleClock(),
        now_fn=lambda: NOW,
    )
    mqtt_dispatcher.publish.reset_mock()

    await coordinator.notify_dali2_event(
        device,
        Dali2InputEvent(
            occupancy.OccupancyEvent(
                short_address=DeviceShort(7),
                instance_number=0,
                data=occupancy.OccupancyEvent.EventData(movement=True, occupied=False),
            )
        ),
    )

    assert [call.args for call in mqtt_dispatcher.publish.await_args_list] == [
        (f"/devices/{device.mqtt_id}/controls/movement0", "1")
    ]


# --- State<->setpoint pairing invariant ---------------------------------


def _real_control_ids() -> set[str]:
    """Every control id the real builders create across the level + DT8 quantity sets.

    Fully building each device type needs a live driver, so the level set is assembled like
    ``DaliDevice.get_common_mqtt_controls`` and the DT8 sets come from their builders directly."""
    curve = DimmingCurveState()
    control_sets = [
        [ActualLevelControl(curve), WantedLevelControl(curve), *make_controls()],
        rgbwaf_mqtt_controls(only_setup_controls=False),
        tc_mqtt_controls(Type8TcLimits(MASK_2BYTES, MASK_2BYTES)),
        xy_mqtt_controls(),
        primary_n_mqtt_controls(),
    ]
    return {c.control_info.id for controls in control_sets for c in controls}


def test_setpoint_pairing_matches_real_controls():
    """The state<->setpoint pairing and the ids the real builders create must agree, so a
    renamed or dropped id fails CI instead of silently freezing a group topic."""
    real_ids = _real_control_ids()

    table_ids = set(_SETPOINT_STATE) | set(_SETPOINT_STATE.values())
    orphans = table_ids - real_ids
    assert not orphans, f"pairing ids with no real control: {sorted(orphans)}"

    colour_controls = {cid for cid in real_ids if cid.startswith(("current_", "set_"))}
    uncovered = colour_controls - table_ids
    assert not uncovered, f"current_*/set_* controls not covered by the pairing: {sorted(uncovered)}"
