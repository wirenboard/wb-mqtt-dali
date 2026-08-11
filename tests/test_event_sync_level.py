"""Per-command prediction tests for the event-sync layer.

`ActualLevelControl.apply` and `LastActedControl.apply` are pure against their
injected owner params, so they are exercised directly with lightweight stubs (no
bus). `SettleClock` and the event-poll scheduling additions (`schedule_poll_at`,
randomized re-draw) are tested in isolation too.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from dali.address import GearShort
from dali.gear.general import (
    DAPC,
    Down,
    GoToLastActiveLevel,
    GoToScene,
    Off,
    OnAndStepUp,
    RecallMaxLevel,
    RecallMinLevel,
    StepDown,
    StepDownAndOff,
    StepUp,
    Up,
)

from wb.mqtt_dali.common_dali_device import (
    EVENT_RESYNC_BASE_INTERVAL,
    EVENT_STARTUP_RECONFIRM_DELAY,
    PERIODIC_STATUS_POLL_INTERVAL,
    MqttControlBase,
)
from wb.mqtt_dali.dali_controls import ActualLevelControl, ErrorStatusControl
from wb.mqtt_dali.dali_dimming_curve import DimmingCurveState, DimmingCurveType
from wb.mqtt_dali.dali_type7_parameters import LastActedControl
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.settle_clock import SettleBasis, SettleClock
from wb.mqtt_dali.wbmqtt import ControlMeta, ControlState

ADDR = GearShort(5)


class _SceneStub:  # pylint: disable=too-few-public-methods
    def __init__(self, levels: dict) -> None:
        self._levels = levels

    def scene_level(self, index: int):
        return self._levels.get(index)


def _level_control(max_level=None, min_level=None, scenes=None) -> ActualLevelControl:
    curve = DimmingCurveState()
    curve.curve_type = DimmingCurveType.LINEAR
    return ActualLevelControl(
        curve,
        max_level=SimpleNamespace(value=max_level),
        min_level=SimpleNamespace(value=min_level),
        scene_source=_SceneStub(scenes or {}),
    )


def _fmt(raw: int) -> str:
    curve = DimmingCurveState()
    curve.curve_type = DimmingCurveType.LINEAR
    return f"{curve.get_level(raw):.3f}"


# --- DAPC ----------------------------------------------------------------


def test_dapc_normal_level_predicts_that_level_with_fade():
    control = _level_control()
    assert control.apply(DAPC(ADDR, 200)) == _fmt(200)
    assert control.current_level == 200


def test_dapc_zero_predicts_off_but_still_fades():
    control = _level_control()
    assert control.apply(DAPC(ADDR, 0)) == _fmt(0)
    assert control.current_level == 0


def test_dapc_mask_emits_no_effect():
    control = _level_control()
    control.apply(DAPC(ADDR, 100))  # prime a known level
    assert control.apply(DAPC(ADDR, 255)) is None
    assert control.current_level == 100  # unchanged


# --- Off / Recall --------------------------------------------------------


def test_off_sets_zero_immediately():
    control = _level_control()
    assert control.apply(Off(ADDR)) == _fmt(0)
    assert control.current_level == 0


def test_recall_max_uses_device_max_level():
    control = _level_control(max_level=240)
    assert control.apply(RecallMaxLevel(ADDR)) == _fmt(240)
    assert _level_control().apply(RecallMaxLevel(ADDR)) is None  # MAX unknown -> poll only


def test_recall_min_uses_device_min_level():
    control = _level_control(min_level=20)
    assert control.apply(RecallMinLevel(ADDR)) == _fmt(20)
    assert _level_control().apply(RecallMinLevel(ADDR)) is None


# --- GoToScene / GoToLastActiveLevel -------------------------------------


def test_goto_scene_uses_cached_scene_level():
    control = _level_control(scenes={3: 120})
    assert control.apply(GoToScene(ADDR, 3)) == _fmt(120)
    assert control.current_level == 120


def test_goto_scene_masked_scene_polls_only():
    control = _level_control(scenes={})  # scene 4 unknown
    assert control.apply(GoToScene(ADDR, 4)) is None


def test_goto_last_active_polls_without_optimistic_value():
    """GoToLastActiveLevel is not predicted (rarely emitted, and it would need separate
    last-active tracking) — the level is left to the confirmation poll."""
    control = _level_control()
    control.apply(DAPC(ADDR, 90))
    control.apply(Off(ADDR))
    assert control.apply(GoToLastActiveLevel(ADDR)) is None


# --- Up / Down (not predicted) -------------------------------------------


def test_up_polls_without_optimistic_value():
    control = _level_control()
    control.apply(DAPC(ADDR, 100))
    assert control.apply(Up(ADDR)) is None


def test_down_polls_without_optimistic_value():
    control = _level_control()
    control.apply(DAPC(ADDR, 100))
    assert control.apply(Down(ADDR)) is None


# --- Step commands -------------------------------------------------------


def test_step_up_increments_from_known_level():
    control = _level_control(max_level=254)
    control.apply(DAPC(ADDR, 100))
    assert control.apply(StepUp(ADDR)) == _fmt(101)


def test_step_down_decrements_from_known_level():
    control = _level_control(min_level=1)
    control.apply(DAPC(ADDR, 100))
    assert control.apply(StepDown(ADDR)) == _fmt(99)


def test_step_down_and_off_turns_off_at_min():
    control = _level_control(min_level=10)
    control.apply(DAPC(ADDR, 10))  # cur == MIN
    assert control.apply(StepDownAndOff(ADDR)) == _fmt(0)


def test_on_and_step_up_turns_on_from_off():
    control = _level_control(max_level=254, min_level=15)
    control.apply(DAPC(ADDR, 0))  # off
    assert control.apply(OnAndStepUp(ADDR)) == _fmt(15)


def test_step_without_known_level_polls_only():
    """Step commands need the current level; without it (never seen) -> poll only."""
    control = _level_control(max_level=254, min_level=1)
    assert control.apply(StepUp(ADDR)) is None


# --- Type 7 last_acted ---------------------------------------------------


def test_last_acted_predicted_from_threshold_crossing():
    """Each switch threshold crossing yields its code: up-on(1)/up-off(2) on the wider
    band and down-on(3)/down-off(4) on the narrower one; no transition predicts nothing;
    with the thresholds unread, no value is predicted (poll only)."""
    control = LastActedControl(
        up_on=SimpleNamespace(value=150),
        up_off=SimpleNamespace(value=80),
        down_on=SimpleNamespace(value=40),
        down_off=SimpleNamespace(value=20),
    )
    assert control.apply(100, 200) == "1"  # crosses up-on upward
    assert control.apply(200, 50) == "2"  # crosses up-off downward
    assert control.apply(30, 100) == "3"  # rising crosses down-on only (below up-on)
    assert control.apply(30, 10) == "4"  # falling crosses down-off only (above up-off)
    assert control.apply(100, 100) is None  # no transition

    unread = LastActedControl(
        up_on=SimpleNamespace(value=None),
        up_off=SimpleNamespace(value=None),
        down_on=SimpleNamespace(value=None),
        down_off=SimpleNamespace(value=None),
    )
    assert unread.apply(10, 250) is None  # thresholds unknown -> poll only


# --- SettleClock ---------------------------------------------------------


def test_settle_clock_bases():
    clock = SettleClock()
    assert clock.settle_for(SettleBasis.IMMEDIATE) < clock.settle_for(SettleBasis.STEP_WINDOW)
    # fade code 8 == 8.0s, plus margin
    assert clock.settle_for(SettleBasis.FADE, 8) > 8.0
    # unknown fade (None or an out-of-table code) -> the same default delay, longer than
    # the immediate margin and independent of the (missing) code.
    assert clock.settle_for(SettleBasis.FADE, None) == clock.settle_for(SettleBasis.FADE, 99)
    assert clock.settle_for(SettleBasis.FADE, None) > clock.settle_for(SettleBasis.IMMEDIATE)


def test_settle_clock_horizon_bounded_by_max_fade():
    clock = SettleClock()
    # longest fade code is 15 (~90.5s); settle stays within a small margin of it.
    assert clock.settle_for(SettleBasis.FADE, 15) < 92.0


# --- Confirmation scheduling / randomized re-draw ------------------------


def _event_control() -> MqttControlBase:
    return MqttControlBase(
        ControlInfo("c", ControlState(ControlMeta(read_only=True), "0")),
        poll_interval=EVENT_RESYNC_BASE_INTERVAL,
        randomize_poll_interval=True,
        startup_reconfirm=True,
    )


def _polled_event_control(at: float) -> MqttControlBase:
    """Event control past its first poll, due one exact base interval later — no startup
    reconfirm, no jitter, so a confirmation has something unambiguous to move."""
    control = _event_control()
    control.next_due_at = at + EVENT_RESYNC_BASE_INTERVAL
    return control


def test_schedule_poll_at_latest_command_wins():
    """schedule_poll_at lets the latest command's settle win — a later confirm
    replaces an earlier pulled-in one (so an immediate-then-fade burst confirms after the
    fade, not on the immediate command's short window), and it overrides the periodic
    schedule outright: rescheduling is a deliberate interference with the period."""
    control = _polled_event_control(1000.0)
    control.schedule_poll_at(1002.0)  # immediate command -> +2s
    assert control.next_due_at == 1002.0
    control.schedule_poll_at(1008.0)  # fade command -> +8s, overrides the earlier
    assert control.next_due_at == 1008.0
    control.schedule_poll_at(5000.0)  # not capped at the re-sync base
    assert control.next_due_at == 5000.0


def test_first_read_not_deferred_by_schedule_poll_at():
    """Rescheduling overrides the period, but not before the first poll: bus traffic at
    service start must not push a control's very first read into the future."""
    control = _event_control()
    control.schedule_poll_at(50.0)
    assert control.is_poll_due(0.0) is True
    # Once polled, the same call does move the poll on.
    control.schedule_next_periodic_poll(polled_at=0.0)
    control.schedule_poll_at(50.0)
    assert control.next_due_at == 50.0


def test_startup_polls_then_reconfirms_after_settle():
    """At service start, an event control's first poll fills the topic and schedules one
    extra confirmation poll at the startup settle delay (in case start caught a transition
    mid-fade); the next poll then settles to the long re-sync interval. This per-control
    first-read reconfirm replaces any global READY pass."""
    control = _event_control()
    assert control.is_poll_due(0.0) is True
    control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=0.0)
    assert control.next_due_at == EVENT_STARTUP_RECONFIRM_DELAY
    # The reconfirm poll itself is not a first poll -> back on the long re-sync interval.
    control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=6.0)
    interval = control.next_due_at - 6.0
    assert EVENT_RESYNC_BASE_INTERVAL * 0.7 <= interval <= EVENT_RESYNC_BASE_INTERVAL * 1.3


def test_startup_reconfirm_only_ever_pulls_the_poll_closer():
    """The reconfirm is a `min`, not an assignment: a pollable whose base interval is shorter
    than the startup delay keeps its own interval instead of having its first read pushed out.
    """
    control = MqttControlBase(
        ControlInfo("short", ControlState(ControlMeta(read_only=True), "0")),
        poll_interval=2.0,
        randomize_poll_interval=True,
        startup_reconfirm=True,
    )
    control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=0.0)
    assert control.next_due_at <= 2.0 * 1.3


def test_level_and_last_acted_opt_into_the_startup_reconfirm():
    """The two event controls the coordinator confirms carry `startup_reconfirm`, so a level
    captured mid-fade at service start is corrected 6 s later, not one re-sync interval later.
    """
    for control in (ActualLevelControl(DimmingCurveState()), LastActedControl()):
        control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=0.0)
        assert control.next_due_at == EVENT_STARTUP_RECONFIRM_DELAY


def test_resync_interval_randomized_within_bounds():
    """Once the startup reconfirm is consumed, each completed poll re-draws the re-sync
    interval within base ±30%, and draws differ across controls (no synchronized storm)."""
    intervals = set()
    for _ in range(50):
        control = _event_control()
        # First poll schedules the startup reconfirm; the next lands on the re-sync interval.
        control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=0.0)
        control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=10.0)
        interval = control.next_due_at - 10.0
        low = EVENT_RESYNC_BASE_INTERVAL * 0.7
        high = EVENT_RESYNC_BASE_INTERVAL * 1.3
        assert low <= interval <= high
        intervals.add(round(interval, 6))
    assert len(intervals) > 1  # jittered, not constant -> no synchronized storm


def test_event_param_resynced_after_interval():
    """An event control with no events re-syncs no sooner than its drawn re-sync interval."""
    control = _event_control()
    control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=0.0)  # startup reconfirm
    control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=10.0)  # re-sync interval
    interval = control.next_due_at - 10.0
    assert control.is_poll_due(10.0 + interval - 1.0) is False
    assert control.is_poll_due(10.0 + interval + 1.0) is True


def test_rescheduled_poll_restores_the_base_interval_without_jitter():
    """A pollable with no jitter recovers its base interval after a rescheduled poll.

    Restoring it used to be a side effect of the ±30% re-draw, which returns early for
    periodic controls — a confirmation would have left one polling every couple of seconds.
    """
    err = ErrorStatusControl()
    err.schedule_next_periodic_poll(polled_at=0.0)
    err.schedule_poll_at(2.0)
    assert err.is_poll_due(2.0) is True

    err.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=2.0)
    assert err.next_due_at == 2.0 + PERIODIC_STATUS_POLL_INTERVAL
    assert err.is_poll_due(4.0) is False


def test_confirmation_poll_resets_resync_timer():
    """A confirmation poll moves the schedule on from itself, so the background re-sync
    does not fire a second read right behind it."""
    control = _polled_event_control(100.0)
    control.schedule_poll_at(102.0)  # pull confirm to +2s
    control.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=102.0)
    assert control.next_due_at >= 102.0 + EVENT_RESYNC_BASE_INTERVAL * 0.7
    assert control.is_poll_due(103.0) is False  # back on the long interval


def test_periodic_param_still_polled():
    """Periodic controls keep a fixed (un-jittered) interval and are unaffected by event poll logic."""
    err = ErrorStatusControl()
    assert err.randomize_poll_interval is False
    err.next_poll_step(None, ADDR, max_commands=3, default_max_commands=3, now=0.0)
    assert err.next_due_at == 120.0  # fixed interval, no jitter re-draw
    assert err.is_poll_due(119.0) is False
    assert err.is_poll_due(120.0) is True


def test_unsettled_value_corrected_by_resync():
    """A confirm read that lands mid-fade overwrites the optimistic value; a later re-sync
    read corrects it to the settled level (no early re-read needed)."""
    control = _level_control()
    control.apply(DAPC(ADDR, 200))  # optimistic target
    control.format_response(_response(150))  # confirm read mid-fade
    assert control.current_level == 150
    control.format_response(_response(200))  # later re-sync read
    assert control.current_level == 200


def _response(raw: int) -> MagicMock:
    resp = MagicMock()
    resp.raw_value = MagicMock(as_integer=raw)
    return resp
