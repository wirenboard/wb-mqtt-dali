"""Tests for `BusMonitorFrameHandler` reorder behaviour.

The handler must dispatch frames in `frame_counter` order regardless of the
publication order wb-mqtt-serial picks. The original implementation only
handled the narrow `N → N+2 → N+1` reorder pattern; these tests cover the
wider window observed on real stands plus the forward-jump and late-arrival
paths.
"""

import logging
from dataclasses import dataclass, field
from typing import List

import pytest

from wb.mqtt_dali.bus_traffic import BusTrafficCallbacks, BusTrafficItem
from wb.mqtt_dali.wbdali import BUS_MONITOR_REORDER_WINDOW, BusMonitorFrameHandler

# pylint: disable=redefined-outer-name

_LOGGER_NAME = "test.bus_monitor_frame_handler"


def _raw(fc: int, *, data: int = 0, frame_length: int = 24) -> int:
    """Pack a bus_monitor payload (only the fields the handler reads)."""
    return data | (frame_length << 32) | (fc << 48)


class _MockMessage:  # pylint: disable=too-few-public-methods
    def __init__(self, raw: int) -> None:
        self.payload = str(raw).encode()
        self.topic = "test/topic"
        self.retain = False


@dataclass
class _Harness:
    handler: BusMonitorFrameHandler
    received: List[BusTrafficItem] = field(default_factory=list)

    def feed(self, *fcs: int) -> None:
        for fc in fcs:
            self.handler.handle(_MockMessage(_raw(fc)))

    def dispatched_counters(self) -> List[int]:
        return [item.frame_counter for item in self.received]


@pytest.fixture
def harness() -> _Harness:
    callbacks = BusTrafficCallbacks(gateway_queue_size=16)
    received: List[BusTrafficItem] = []
    callbacks.register(received.append)
    handler = BusMonitorFrameHandler(callbacks, logging.getLogger(_LOGGER_NAME), dev_inst_map=None)
    return _Harness(handler=handler, received=received)


def _warning_messages(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_in_order_dispatches_immediately_no_warnings(harness, caplog):
    """Strict-order arrivals are forwarded with no buffering and no warnings."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, 101, 102, 103)
    assert harness.dispatched_counters() == [100, 101, 102, 103]
    assert _warning_messages(caplog) == []


def test_simple_swap_reordered_to_counter_order(harness, caplog):
    """`N → N+2 → N+1` arrives swapped; handler must dispatch in counter order."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, 102, 101)
    assert harness.dispatched_counters() == [100, 101, 102]
    assert _warning_messages(caplog) == []


def test_three_step_swap_reordered(harness, caplog):
    """`N → N+2 → N+3 → N+1` — the stand's actual pattern. The original
    implementation prematurely dispatched N+2 and emitted a false warning.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, 102, 103, 101)
    assert harness.dispatched_counters() == [100, 101, 102, 103]
    assert _warning_messages(caplog) == []


def test_reorder_then_continues_in_order(harness, caplog):
    """After a reorder episode, subsequent in-order frames must keep flowing."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, 102, 103, 101, 104, 105)
    assert harness.dispatched_counters() == [100, 101, 102, 103, 104, 105]
    assert _warning_messages(caplog) == []


def test_real_loss_flushed_when_forward_jump_exceeds_window(harness, caplog):
    """If a frame in the middle of a reorder run never arrives and a later
    frame lands beyond the reorder window, the handler concedes the gap,
    dispatches buffered frames in counter order, and warns once.
    """
    # Receive 100, then jump to 102..(102 + WINDOW). N+1 (=101) is gone.
    # The last entry, 102+WINDOW, sits past WINDOW relative to 101 and
    # triggers the forward-jump branch — that's what flushes the buffer.
    future = [102 + i for i in range(BUS_MONITOR_REORDER_WINDOW + 1)]
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, *future)
    assert harness.dispatched_counters() == [100, *future]
    warnings = _warning_messages(caplog)
    assert len(warnings) == 1
    # Warning carries the boundary counters (last dispatched and first buffered)
    # and the count of missed frames in between.
    assert "from 100 to 102" in warnings[0]
    assert "1 frame(s) missed" in warnings[0]


def test_consecutive_run_after_gap_does_not_stall(harness, caplog):
    """A single lost frame followed by a tight run of `ring_size - 1` frames
    must trigger a flush on the next arrival, not buffer it. With a 4-slot
    gateway ring the missing slot is overwritten by the time we see the
    fourth ahead-of-expected frame, so further buffering would stall dispatch
    until the next bus event (potentially many seconds on a quiet bus).
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, 102, 103, 104, 105)
    assert harness.dispatched_counters() == [100, 102, 103, 104, 105]
    warnings = _warning_messages(caplog)
    assert len(warnings) == 1
    assert "from 100 to 102" in warnings[0]
    assert "1 frame(s) missed" in warnings[0]


def test_forward_jump_beyond_window_warns_and_dispatches(harness, caplog):
    """A counter that jumps past `WINDOW` slots in one go is a real gap."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, 100 + BUS_MONITOR_REORDER_WINDOW + 5)
    assert harness.dispatched_counters() == [100, 100 + BUS_MONITOR_REORDER_WINDOW + 5]
    assert len(_warning_messages(caplog)) == 1


def test_backward_jump_is_dropped_with_warning(harness, caplog):
    """A frame whose counter went backward past the reorder window indicates
    a gateway anomaly (republished frame or oversized wb-mqtt-serial reorder).
    The handler must not dispatch it — splicing it in after subsequent counters
    have already gone out is meaningless — and must warn.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100, 101, 102, 103)
        assert _warning_messages(caplog) == []
        # An out-of-order arrival of fc=50 (clearly in the past, no wrap).
        harness.handler.handle(_MockMessage(_raw(50)))
    assert harness.dispatched_counters() == [100, 101, 102, 103]
    warnings = _warning_messages(caplog)
    assert len(warnings) == 1
    assert "backwards" in warnings[0]


def test_first_frame_seeds_expected_counter(harness, caplog):
    """The very first frame seeds the expected counter to fc + 1, so the
    next-in-order frame must not be flagged as a jump.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(0xABCD, 0xABCE, 0xABCF)
    assert harness.dispatched_counters() == [0xABCD, 0xABCE, 0xABCF]
    assert _warning_messages(caplog) == []


def test_counter_wraparound_in_order(harness, caplog):
    """Counter wraps cleanly from 0xFFFF to 0x0000."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(0xFFFE, 0xFFFF, 0x0000, 0x0001)
    assert harness.dispatched_counters() == [0xFFFE, 0xFFFF, 0x0000, 0x0001]
    assert _warning_messages(caplog) == []


def test_counter_wraparound_with_reorder(harness, caplog):
    """Wraparound combined with a one-slot swap still resolves correctly."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(0xFFFE, 0x0000, 0xFFFF, 0x0001)
    assert harness.dispatched_counters() == [0xFFFE, 0xFFFF, 0x0000, 0x0001]
    assert _warning_messages(caplog) == []


def test_forward_jump_flushes_buffered_frames_across_wraparound(harness, caplog):
    """Forward jump beyond the window while the buffer straddles the 16-bit
    wraparound: buffered frames must be dispatched in true counter order
    (0xFFFF before 0x0000), not in numeric order, and the `missed` boundary
    counters must be reported using modular order, not numeric.
    """
    # Seed expected=0xFFFE via the first frame, then buffer 0xFFFF/0x0000/0x0001
    # (all within the reorder window ahead of 0xFFFE, with 0xFFFE itself
    # missing). A frame far enough past the window triggers the flush.
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(0xFFFD, 0xFFFF, 0x0000, 0x0001, 0x0010)
    # The crux of the fix: dispatch order is counter-modular (0xFFFF before
    # 0x0000), not numeric (which would have placed 0x0000/0x0001 first).
    assert harness.dispatched_counters() == [0xFFFD, 0xFFFF, 0x0000, 0x0001, 0x0010]
    warnings = _warning_messages(caplog)
    # Two real gaps: one inside the buffered run (missing 0xFFFE), one between
    # the last buffered frame and the trigger.
    assert any("from 65533 to 65535" in w and "1 frame(s) missed" in w for w in warnings)
    assert any("from 1 to 16" in w and "14 frame(s) missed" in w for w in warnings)


def test_retained_message_ignored(harness):
    """Retained MQTT messages must not be processed (would replay history)."""
    message = _MockMessage(_raw(100))
    message.retain = True
    harness.handler.handle(message)
    assert harness.dispatched_counters() == []
