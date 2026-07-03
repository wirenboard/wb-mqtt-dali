"""EventSyncCoordinator tests: own/foreign commands -> optimistic MQTT
state + confirmation polls, fade tracking, group/broadcast optimism, the aggregate
group topic, and the READY-reentry reconfirm.

Devices are lightweight fakes that expose only the public surface the coordinator
touches (``get_mqtt_control``, ``dt8_handler``, ``fade_param``, ``groups``, ``uid``,
``mqtt_id``). Real controls/handlers are used so prediction logic is exercised; the
DT8 colour handler is initialized through its public ``read_mandatory_info`` with a
fake driver (no private attribute access). A fake clock drives settle timing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dali.address import GearBroadcast, GearGroup, GearShort
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
from dali.gear.general import DAPC, DTR0, DTR1, DTR2, GoToScene, Off, SetFadeTime

from wb.mqtt_dali.colour_sequence_tracker import ColourSequenceTracker
from wb.mqtt_dali.common_dali_device import ControlPollResult
from wb.mqtt_dali.dali_common_parameters import FadeTimeFadeRateParam
from wb.mqtt_dali.dali_controls import (
    ActualLevelControl,
    WantedLevelControl,
    make_controls,
)
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.dali_type7_parameters import LastActedControl
from wb.mqtt_dali.dali_type8_parameters import (
    ColourSettings,
    ColourType,
    Type8Parameters,
)
from wb.mqtt_dali.dali_type8_primary_n import (
    get_mqtt_controls as primary_n_mqtt_controls,
)
from wb.mqtt_dali.dali_type8_rgbwaf import get_mqtt_controls as rgbwaf_mqtt_controls
from wb.mqtt_dali.dali_type8_tc import get_mqtt_controls as tc_mqtt_controls
from wb.mqtt_dali.dali_type8_xy import get_mqtt_controls as xy_mqtt_controls
from wb.mqtt_dali.device_registry import DeviceRegistry
from wb.mqtt_dali.dtr_snapshot import DtrSnapshot
from wb.mqtt_dali.event_sync_coordinator import (
    _COLOUR_MIRROR,
    _OWNED_SETPOINTS,
    EventSyncCoordinator,
)
from wb.mqtt_dali.settle_clock import SettleBasis, SettleClock
from wb.mqtt_dali.virtual_devices import GroupStateSource
from wb.mqtt_dali.wbdali_utils import MASK_2BYTES

NOW = 1000.0
RESYNC_INTERVAL = 300.0


class _SceneStub:  # pylint: disable=too-few-public-methods
    def __init__(self, levels: dict) -> None:
        self._levels = levels

    def scene_level(self, index: int):
        return self._levels.get(index)


class _GearDevice:  # pylint: disable=too-many-instance-attributes,too-few-public-methods
    """Public-surface fake of a DALI gear device for the coordinator."""

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
        self.address = SimpleNamespace(short=short)
        self.uid = f"uid-{short}"
        self.mqtt_id = f"dev-{short}"
        self.name = f"dev{short}"
        self.groups = set(groups)
        self.fade_param = FadeTimeFadeRateParam()
        if fade_code is not None:
            self.fade_param.set_fade_time(fade_code)
        curve = DimmingCurveState()
        curve.curve_type = DimmingCurveType.LINEAR
        self._controls = {
            "actual_level": ActualLevelControl(
                curve,
                max_level=SimpleNamespace(value=max_level),
                min_level=SimpleNamespace(value=min_level),
                scene_source=scene_source if scene_source is not None else _SceneStub({}),
            )
        }
        if last_acted is not None:
            self._controls["last_acted"] = last_acted
        self.dt8_handler = None

    def get_mqtt_control(self, control_id):
        return self._controls.get(control_id)


def _fmt(raw: int) -> str:
    curve = DimmingCurveState()
    curve.curve_type = DimmingCurveType.LINEAR
    return f"{curve.get_level(raw):.3f}"


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
    """Simulate a prior completed poll so poll_no_later_than can pull the next one earlier."""
    control.last_poll_time = NOW
    control.poll_interval = RESYNC_INTERVAL


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


def _published(publisher) -> dict:
    """Map (device_id, control_id) -> last published value."""
    return {(c.args[0], c.args[1]): c.args[2] for c in publisher.set_control_value.await_args_list}


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

    publisher.set_control_value.assert_not_awaited()


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
    assert actual.last_poll_time == NOW
    assert actual.poll_interval == pytest.approx(expected)
    assert actual.poll_interval < RESYNC_INTERVAL


@pytest.mark.asyncio
async def test_fade_time_unknown_uses_default_delay():
    """A fading command on a device with no known fade code schedules its confirmation
    poll at the SettleClock default-delay, not a fade-derived time."""
    device = _GearDevice(5, fade_code=None)
    actual = device.get_mqtt_control("actual_level")
    _prime_poll(actual)
    coordinator, _ = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 100)])

    assert actual.poll_interval == pytest.approx(SettleClock().settle_for(SettleBasis.FADE, None))


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
    assert actual.poll_interval == pytest.approx(SettleClock().settle_for(SettleBasis.FADE, 10))


@pytest.mark.asyncio
async def test_confirmation_poll_is_single_read():
    """After a confirm poll is scheduled and the read happens, the interval re-draws to
    the long base — no early re-read is queued."""
    device = _GearDevice(5, fade_code=2)
    actual = device.get_mqtt_control("actual_level")
    _prime_poll(actual)
    coordinator, _ = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 100)])
    assert actual.poll_interval < RESYNC_INTERVAL  # pulled in for the confirm

    # The read fires; the control re-draws its interval back to the long base.
    actual.next_poll_step(None, GearShort(5), max_commands=3, default_max_commands=3, now=NOW + 2.0)
    assert actual.poll_interval >= RESYNC_INTERVAL * 0.7


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
    actual.apply(DAPC(GearShort(5), 50))  # prime level 50
    _prime_poll(actual)
    _prime_poll(last_acted)
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])  # 50 -> 200 crosses up-on (150)

    assert _published(publisher)[("dev-5", "last_acted")] == "1"
    assert last_acted.poll_interval < RESYNC_INTERVAL  # also confirm-polled


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
    group_device = SimpleNamespace(
        mqtt_id="group-2",
        state_source=GroupStateSource({"actual_level": [member.uid]}),
    )
    coordinator, publisher = _coordinator([member], group_devices={2: group_device})

    await coordinator.apply_commands([DAPC(GearGroup(2), 200)])

    assert _published(publisher)[("group-2", "actual_level")] == _fmt(200)


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
async def test_colour_overlay_carries_forward_prior_components():
    """A second partial colour command overlays onto the first's already-known component
    and republishes it, rather than resetting the unset channel to the MASK sentinel — so
    both coordinates end up published, the first carried forward as a real value."""
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
        c for c in publisher.set_control_value.await_args_list if c.args[1] == "current_x_coordinate"
    ]
    assert len(x_publishes) == 2  # republished on the second command (carried, not dropped as MASK)
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

    publisher.set_control_value.assert_not_awaited()
    assert handler.poll_interval < RESYNC_INTERVAL  # still confirm-polled


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

    publisher.set_control_value.assert_not_awaited()
    assert handler.poll_interval < RESYNC_INTERVAL


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


# --- Representation mirroring --------------------------------------------


class _FullDevice:  # pylint: disable=too-many-instance-attributes,too-few-public-methods
    """Public-surface fake carrying the full set of a quantity's representation controls."""

    def __init__(self, short, controls, dt8_handler=None, groups=()) -> None:
        self.address = SimpleNamespace(short=short)
        self.uid = f"uid-{short}"
        self.mqtt_id = f"dev-{short}"
        self.name = f"dev{short}"
        self.groups = set(groups)
        self.fade_param = FadeTimeFadeRateParam()
        self.dt8_handler = dt8_handler
        self._controls = {c.control_info.id: c for c in controls}

    def get_mqtt_control(self, control_id):
        return self._controls.get(control_id)


def _linear_curve() -> DimmingCurveState:
    curve = DimmingCurveState()
    curve.curve_type = DimmingCurveType.LINEAR
    return curve


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


def _level_device(short=5) -> _FullDevice:
    return _FullDevice(short, _level_controls(_linear_curve()))


def _colour_device(handler, colour_controls, short=5, groups=()) -> _FullDevice:
    return _FullDevice(
        short, [*_level_controls(_linear_curve()), *colour_controls], dt8_handler=handler, groups=groups
    )


def _level_response(raw: int) -> MagicMock:
    resp = MagicMock()
    resp.raw_value = MagicMock(as_integer=raw)
    return resp


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
    device = _colour_device(handler, rgbwaf_mqtt_controls(only_setup_controls=False))
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
    device = _colour_device(handler, tc_mqtt_controls(100, 500))
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
    device = _colour_device(handler, rgbwaf_mqtt_controls(only_setup_controls=False))
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
    device = _colour_device(handler, rgbwaf_mqtt_controls(only_setup_controls=False))
    device.get_mqtt_control("actual_level").format_response(_level_response(180))  # store raw
    coordinator, publisher = _coordinator([device])

    results = [
        ControlPollResult("actual_level", _fmt(180)),
        ControlPollResult("current_rgb", "1;2;3"),
        ControlPollResult("current_white", "4"),
    ]
    await coordinator.publish_poll_setpoint_mirror(device, results)

    published = _published(publisher)
    assert published[("dev-5", "wanted_level")] == "71"  # round(_fmt(180) == "70.866")
    assert published[("dev-5", "dapc")] == "180"
    assert published[("dev-5", "set_rgb")] == "1;2;3"
    assert published[("dev-5", "set_white")] == "4"


@pytest.mark.asyncio
async def test_poll_readback_skips_errored_and_none_results():
    """A poll batch mixing a failed read (error="r", value=None), a value-less read
    (value=None, no error) and a good read mirrors setpoints only for the good one: the
    errored/None entries are skipped, so no stale value is painted and round(float(None))
    never runs on the level branch."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _colour_device(handler, rgbwaf_mqtt_controls(only_setup_controls=False))
    coordinator, publisher = _coordinator([device])

    results = [
        ControlPollResult("actual_level", error="r"),  # failed read -> no level setpoints
        ControlPollResult("current_white", None),  # value-less read -> no set_white
        ControlPollResult("current_rgb", "1;2;3"),  # good read -> mirrors
    ]
    await coordinator.publish_poll_setpoint_mirror(device, results)

    published = _published(publisher)
    assert ("dev-5", "wanted_level") not in published
    assert ("dev-5", "dapc") not in published
    assert ("dev-5", "set_white") not in published
    assert published[("dev-5", "set_rgb")] == "1;2;3"


@pytest.mark.asyncio
async def test_level_suppressed_while_read_error():
    """While the level's read poll is failing, no level representation is published (so the
    standing /meta/error=r is not cleared)."""
    device = _level_device()
    device.get_mqtt_control("actual_level").read_error = True
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands([DAPC(GearShort(5), 200)])

    publisher.set_control_value.assert_not_awaited()


@pytest.mark.asyncio
async def test_colour_suppressed_while_read_error():
    """While a colour state control's read poll is failing, neither it nor its setpoint is
    published."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    device = _colour_device(handler, rgbwaf_mqtt_controls(only_setup_controls=False))
    device.get_mqtt_control("current_rgb").read_error = True
    coordinator, publisher = _coordinator([device])

    await coordinator.apply_commands(
        [DTR0(10), DTR1(20), DTR2(30), SetTemporaryRGBDimLevel(GearShort(5)), Activate(GearShort(5))]
    )

    published = _published(publisher)
    assert ("dev-5", "current_rgb") not in published
    assert ("dev-5", "set_rgb") not in published


@pytest.mark.asyncio
async def test_read_error_no_flicker():
    """A stream of commands while a read error stands produces no value publish — so nothing
    clears /meta/error and it never flickers r <-> "" under live traffic."""
    device = _level_device()
    device.get_mqtt_control("actual_level").read_error = True
    coordinator, publisher = _coordinator([device])

    for _ in range(5):
        await coordinator.apply_commands([DAPC(GearShort(5), 200), Off(GearShort(5))])

    publisher.set_control_value.assert_not_awaited()
    publisher.set_control_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_topic_single_publish():
    """A group command moves each group state topic (level and colour) exactly once and
    mirrors the pinned member's owned setpoints (wanted_level/dapc/set_*) once each, with
    the same value — so the group card's setpoints track the member whose state it shows."""
    handler = await _make_colour_handler(ColourType.RGBWAF)
    member = _colour_device(handler, rgbwaf_mqtt_controls(only_setup_controls=False), short=3, groups={2})
    group_controls = {"actual_level", "current_rgb", "wanted_level", "dapc", "set_rgb"}
    group_device = SimpleNamespace(
        mqtt_id="group-2",
        state_source=GroupStateSource({"actual_level": [member.uid], "current_rgb": [member.uid]}),
        get_mqtt_control=lambda cid: object() if cid in group_controls else None,
    )
    coordinator, publisher = _coordinator([member], group_devices={2: group_device})

    await coordinator.apply_commands([DAPC(GearGroup(2), 200)])
    await coordinator.apply_commands(
        [DTR0(10), DTR1(20), DTR2(30), SetTemporaryRGBDimLevel(GearGroup(2)), Activate(GearGroup(2))]
    )

    group_values: dict[str, list[str]] = {}
    for call in publisher.set_control_value.await_args_list:
        if call.args[0] == "group-2":
            group_values.setdefault(call.args[1], []).append(call.args[2])
    published = _published(publisher)
    # Each state topic and each owned setpoint moves exactly once...
    for control_id in ("actual_level", "current_rgb", "wanted_level", "dapc", "set_rgb"):
        assert len(group_values.get(control_id, [])) == 1, control_id
    # ...and the setpoints carry the pinned member's value verbatim.
    for setpoint in ("wanted_level", "dapc", "set_rgb"):
        assert published[("group-2", setpoint)] == published[("dev-3", setpoint)]


# --- State<->setpoint pairing invariant ---------------------------------


def _real_control_ids() -> set[str]:
    """Every control id the real builders create across the level + DT8 quantity sets.

    Fully building each device type needs a live driver, so the level set is assembled like
    ``DaliDevice.get_common_mqtt_controls`` and the DT8 sets come from their builders directly."""
    curve = DimmingCurveState()
    control_sets = [
        [ActualLevelControl(curve), WantedLevelControl(curve), *make_controls()],
        rgbwaf_mqtt_controls(only_setup_controls=False),
        tc_mqtt_controls(MASK_2BYTES, MASK_2BYTES),
        xy_mqtt_controls(),
        primary_n_mqtt_controls(),
    ]
    return {c.control_info.id for controls in control_sets for c in controls}


def test_coordinator_tables_match_real_controls():
    """The coordinator's mirror/ownership tables and the ids the real builders create must
    agree, so a renamed or dropped id fails CI instead of silently freezing a topic:
    (a) every id the tables reference is a real control (no orphan constant), and (b) every
    ``current_*``/``set_*`` colour control the builders create is covered by the tables."""
    real_ids = _real_control_ids()

    table_ids = set(_COLOUR_MIRROR) | set(_COLOUR_MIRROR.values()) | set(_OWNED_SETPOINTS)
    orphans = table_ids - real_ids
    assert not orphans, f"table ids with no real control: {sorted(orphans)}"

    colour_controls = {cid for cid in real_ids if cid.startswith(("current_", "set_"))}
    uncovered = colour_controls - table_ids
    assert not uncovered, f"current_*/set_* controls not covered by tables: {sorted(uncovered)}"
