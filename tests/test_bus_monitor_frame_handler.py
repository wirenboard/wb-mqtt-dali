"""Tests for `BusMonitorFrameHandler`.

The handler must dispatch frames in `frame_counter` order regardless of the
publication order wb-mqtt-serial picks. The original implementation only
handled the narrow `N → N+2 → N+1` reorder pattern; these tests cover the
wider window observed on real stands plus the forward-jump and late-arrival
paths.

Beyond ordering they cover what a slot may contain and recovery from a corrupted
counter that parked the expected position out of the stream's reach (SOFT-7351).
"""

import logging
from dataclasses import dataclass, field
from typing import List

import pytest
from dali.frame import BackwardFrame, BackwardFrameError

from wb.mqtt_dali.bus_traffic import BusTrafficCallbacks, BusTrafficItem
from wb.mqtt_dali.wbdali import (
    BUS_MONITOR_REORDER_WINDOW,
    BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND,
    BUS_MONITOR_RING_SIZE,
    BusMonitorFrameHandler,
)

# pylint: disable=redefined-outer-name

_LOGGER_NAME = "test.bus_monitor_frame_handler"


def _raw(
    fc: int,
    *,
    data: int = 0,
    frame_length: int = 24,
    is_backward: bool = False,
    is_broken: bool = False,
) -> int:
    """Pack a bus_monitor payload (only the fields the handler reads)."""
    return data | (frame_length << 32) | (int(is_backward) << 40) | (int(is_broken) << 41) | (fc << 48)


# Verbatim slot value from the field log (SOFT-7351): frame counter 220, length
# byte 0xFF, no data. The old handler turned it into a 255-bit forward frame.
_FIELD_INVALID_SLOT = 0x00DC00FF00000000


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


def test_cleared_control_value_is_ignored(harness, caplog):
    """Empty payload (wb-mqtt-serial clearing the control) is "no value", not a
    malformed frame, so it must not be reported as a parse failure.
    """
    message = _MockMessage(0)
    message.payload = b""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        harness.handler.handle(message)
    assert harness.dispatched_counters() == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_unparsable_payloads_are_reported(harness, caplog):
    """Text that is not a number, bytes that do not decode and a value of an
    unconvertible type all come out as one exception type and one log line — an
    unexpected type escaping the handler is what wedged it in the field.
    """
    payloads = (b"not-a-number", b"\xff\xfe", [1, 2])
    with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
        harness.feed(100)
        for payload in payloads:
            message = _MockMessage(0)
            message.payload = payload
            harness.handler.handle(message)
        harness.feed(101)
    assert harness.dispatched_counters() == [100, 101]
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == len(payloads)


def test_payload_wider_than_the_slot_register_is_rejected(harness, caplog):
    """A payload that does not fit the 64-bit slot register is not a slot at all:
    it must be reported and dropped without disturbing the counter state.
    """
    with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
        harness.feed(100)
        harness.handler.handle(_MockMessage(1 << 64))
        harness.feed(101)
    assert harness.dispatched_counters() == [100, 101]
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_first_real_frame_after_zero_slots_seeds_counter(harness, caplog):
    """Enabling Fast Modbus events publishes all four ring slots, and on a bus with no
    sporadic frames yet that burst is four zeroes — it used to raise `ValueError` on the
    first slot and report the rest as counters going backwards. Zero slots must also not
    seed the expected counter, otherwise the first real frame looks like it came from the
    past; the next frame seeds it instead.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        for _ in range(BUS_MONITOR_RING_SIZE):
            harness.handler.handle(_MockMessage(0))
        harness.feed(500, 501, 502)
    assert harness.dispatched_counters() == [500, 501, 502]
    assert _warning_messages(caplog) == []


def test_invalid_frame_length_is_dropped(harness, caplog):
    """The field-log slot (length byte 0xFF) must be reported and dropped, not
    published as a 255-bit forward frame.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.handler.handle(_MockMessage(_FIELD_INVALID_SLOT))
    assert harness.dispatched_counters() == []
    warnings = _warning_messages(caplog)
    assert len(warnings) == 1
    assert "no DALI frame" in warnings[0]
    assert "255 bit(s)" in warnings[0]


def test_invalid_slot_on_time_advances_the_counter(harness, caplog):
    """A dropped slot still occupies its number in the ring, so the next valid frame must
    not be reported as arriving after a gap. Arriving exactly on time it must also drain
    the frames waiting behind it, or they sit in the buffer until some later jump flushes
    them.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100)
        harness.feed(102)  # ahead within the window — buffered
        harness.handler.handle(_MockMessage(_raw(101, frame_length=255)))
        harness.feed(103)  # no gap reported: 101 was accounted for
    assert harness.dispatched_counters() == [100, 102, 103]
    assert all("no DALI frame" in w for w in _warning_messages(caplog))


def test_invalid_slot_outside_the_window_is_dropped_whole(harness, caplog):
    """An invalid slot's counter is untrustworthy too, so outside the reorder window it
    goes with the slot: honouring one far ahead would park the expected position out of
    reach of every real frame, and one far behind would report a backward jump and count
    towards a resynchronisation.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100)
        harness.handler.handle(_MockMessage(_raw(30000, frame_length=255)))  # far ahead
        harness.feed(101, 102)
        for _ in range(BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND):
            harness.handler.handle(_MockMessage(_raw(50, frame_length=255)))  # far behind
        harness.feed(103)
    assert harness.dispatched_counters() == [100, 101, 102, 103]
    assert all("no DALI frame" in w for w in _warning_messages(caplog))


def test_invalid_slot_does_not_seed_counter(harness, caplog):
    """With no stream yet there is nothing to judge a counter against, so the
    first valid frame seeds it, not an invalid slot.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.handler.handle(_MockMessage(_raw(30000, frame_length=255)))
        harness.feed(100, 101)
    assert harness.dispatched_counters() == [100, 101]
    warnings = _warning_messages(caplog)
    assert len(warnings) == 1
    assert "no DALI frame" in warnings[0]


def test_stream_resyncs_after_bogus_forward_jump(harness, caplog):
    """A corrupted counter drags the expected position far ahead of the real
    stream, where every following frame reads as "behind" and gets dropped. Enough
    of them must resynchronise the stream instead of waiting out a 16-bit counter wrap.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100)
        harness.feed(30000)  # plausible length, corrupted counter
        # The real stream keeps going where it was; each of these reads as behind.
        harness.feed(*range(101, 101 + BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND))
        harness.feed(105, 106)
    resync_fc = 100 + BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND
    assert harness.dispatched_counters() == [100, 30000, resync_fc, 105, 106]
    warnings = _warning_messages(caplog)
    # The frame that reached the threshold is reported as the resync, not as one more drop.
    assert len([w for w in warnings if "backwards" in w]) == BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND - 1
    assert len([w for w in warnings if f"resynchronising to fc={resync_fc}" in w]) == 1


def test_reordered_stream_resyncs_after_bogus_forward_jump(harness, caplog):
    """Recovery must not depend on the stream arriving in counter order: with the expected
    position parked out of reach, every frame reads as behind whatever order it comes in.
    Counting only frames ahead of the previous one used to silence recovery here entirely.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100)
        harness.feed(30000)  # plausible length, corrupted counter
        harness.feed(101, 103, 102, 104)  # the real stream, reordered as on the stand
        harness.feed(106, 105)  # the reorder buffer works again from the new position
    assert harness.dispatched_counters() == [100, 30000, 104, 105, 106]
    assert len([w for w in _warning_messages(caplog) if "resynchronising to fc=104" in w]) == 1


def test_republished_ring_slots_do_not_trigger_resync(harness, caplog):
    """A republished ring lands up to `BUS_MONITOR_RING_SIZE` frames behind expected.
    They are stale, not evidence of a wrong position: each is reported and dropped, none
    resynchronises the stream backwards.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(*range(100, 100 + BUS_MONITOR_RING_SIZE))
        # The ring replays the same frames, all now behind expected.
        harness.feed(*range(100, 100 + BUS_MONITOR_RING_SIZE))
        harness.feed(100 + BUS_MONITOR_RING_SIZE)
    assert harness.dispatched_counters() == [
        *range(100, 100 + BUS_MONITOR_RING_SIZE),
        100 + BUS_MONITOR_RING_SIZE,
    ]
    warnings = _warning_messages(caplog)
    assert len(warnings) == BUS_MONITOR_RING_SIZE
    assert all("backwards" in w for w in warnings)


def test_resync_needs_several_distinct_far_behind_frames(harness, caplog):
    """A single frame far behind expected is an anomaly, not a stream. Here each is
    followed by a frame arriving on time, which proves the position is still good and
    discards what was collected, so the threshold is never reached.
    """
    behind = range(100, 100 + BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND * 2)
    ontime = range(30001, 30001 + BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND * 2)
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(30000)  # seeds the expected counter at 30001
        for far, fc in zip(behind, ontime):
            harness.feed(far)  # far behind, a counter not seen before
            harness.feed(fc)  # lands where expected — discards the far-behind frames
    assert harness.dispatched_counters() == [30000, *ontime]
    warnings = _warning_messages(caplog)
    assert len(warnings) == len(behind)
    assert all("backwards" in w for w in warnings)


def test_broken_backward_frame_keeps_its_error_flag(harness):
    """A backward frame received with a framing error must arrive as
    `BackwardFrameError`, not as a plain frame with the error lost.
    """
    harness.handler.handle(
        _MockMessage(_raw(100, data=0x2A, frame_length=8, is_backward=True, is_broken=True))
    )
    assert harness.dispatched_counters() == [100]
    assert isinstance(harness.received[0].request, BackwardFrameError)
    assert harness.received[0].request.error is True


def test_far_behind_frames_discarded_by_frame_buffered_within_window(harness, caplog):
    """An ahead-of-expected frame that goes to the reorder buffer proves the position is
    still reachable and must discard the far-behind counters collected so far. Nothing
    here lands on time and nothing jumps, so the window is the only place that can do it.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(30000)  # seeds the expected counter at 30001
        for offset in range(1, BUS_MONITOR_REORDER_WINDOW + 1):
            harness.feed(100 + offset)  # far behind, a counter not seen before
            harness.feed(30001 + offset)  # ahead within the window — buffered
        harness.feed(100)  # would complete the threshold without those discards
    # The buffered frames stay in the buffer: nothing arrived on time to release them.
    assert harness.dispatched_counters() == [30000]
    assert not any("resynchronising" in w for w in _warning_messages(caplog))


def test_far_behind_frames_discarded_by_forward_jump(harness, caplog):
    """A forward jump beyond the window also proves the stream is reachable, so it
    must discard the far-behind counters too.
    """
    jumps = [30010 + offset * 10 for offset in range(BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND * 2)]
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(30000)
        for far, jump in enumerate(jumps, start=100):
            harness.feed(far)  # far behind, a counter not seen before
            harness.feed(jump)  # jump beyond the window — dispatched, discards them
    assert harness.dispatched_counters() == [30000, *jumps]
    assert not any("resynchronising" in w for w in _warning_messages(caplog))


def test_resync_across_wraparound(harness, caplog):
    """The resync commit is modular too: a bogus counter that parks expected just
    below the wrap must still be recovered from.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(0xFFFD)
        harness.feed(0x7000)  # bogus counter, parks expected at 0x7001
        behind = [(0xFFFE + i) % 0x10000 for i in range(BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND)]
        harness.feed(*behind)
        harness.feed(0x0002, 0x0003)
    resync_fc = behind[-1]
    assert harness.dispatched_counters() == [0xFFFD, 0x7000, resync_fc, 0x0002, 0x0003]
    assert any(f"resynchronising to fc={resync_fc}" in w for w in _warning_messages(caplog))


def test_republish_order_change_does_not_resync(harness, caplog):
    """Republish bursts need not keep the slot order between reads. Only distinct
    counters count, so a ring replaying the same ones in any order cannot reach the
    threshold — its newest slot discards them from whatever position in the burst.
    """
    ring = [100, 140, 180, 220]
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(*ring)
        harness.feed(100, 140, 220, 180)
        harness.feed(100, 140, 180, 220)
    assert harness.dispatched_counters() == ring
    assert not any("resynchronising" in w for w in _warning_messages(caplog))


def test_resync_clears_buffer_and_far_behind_frames(harness, caplog):
    """Resynchronising must leave no state from the position it abandoned: a slot buffered
    there would later be flushed out of order, and counters left at the threshold would
    let the next single frame resync on its own.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(100)
        harness.feed(30000)  # corrupted counter, parks expected at 30001
        harness.feed(30002)  # buffered at the bogus position
        behind = range(101, 101 + BUS_MONITOR_RESYNC_AFTER_FRAMES_BEHIND)
        harness.feed(*behind)  # the real stream — resyncs on the last of them
        harness.feed(50)  # a lone far-behind frame must not resync by itself
        harness.feed(130)  # forward jump: nothing stale may surface with it
    resync_fc = behind[-1]
    assert harness.dispatched_counters() == [100, 30000, resync_fc, 130]
    assert len([w for w in _warning_messages(caplog) if "resynchronising" in w]) == 1


def test_invalid_slot_is_reported_every_time(harness, caplog):
    """Reporting of dropped slots is not suppressed on repeats — a systematic
    mismatch with the firmware must stay visible in the log.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        for fc in (100, 101, 102):
            harness.handler.handle(_MockMessage(_raw(fc, frame_length=255)))
    assert harness.dispatched_counters() == []
    assert len(_warning_messages(caplog)) == 3


def test_backward_frame_with_wrong_length_is_dropped(harness, caplog):
    """A backward frame always carries 8 data bits."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.handler.handle(_MockMessage(_raw(100, frame_length=16, is_backward=True)))
    assert harness.dispatched_counters() == []
    assert len(_warning_messages(caplog)) == 1


def test_all_dali_frame_lengths_are_published(harness, caplog):
    """Every length a DALI bus can carry stays supported: FF16/24/25 and BF8."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.handler.handle(_MockMessage(_raw(10, data=0xFF00, frame_length=16)))
        harness.handler.handle(_MockMessage(_raw(11, data=0x018802, frame_length=24)))
        harness.handler.handle(_MockMessage(_raw(12, data=0x1234567, frame_length=25)))
        harness.handler.handle(_MockMessage(_raw(13, data=0x2A, frame_length=8, is_backward=True)))
    assert harness.dispatched_counters() == [10, 11, 12, 13]
    assert [len(item.request) for item in harness.received] == [16, 24, 25, 8]
    assert [item.request.as_integer for item in harness.received] == [
        0xFF00,
        0x018802,
        0x1234567,
        0x2A,
    ]
    # A clean slot must not come out flagged: everything downstream of the monitor
    # is gated on `not request.error`.
    assert [item.request.error for item in harness.received] == [False] * 4
    assert isinstance(harness.received[3].request, BackwardFrame)
    assert not isinstance(harness.received[3].request, BackwardFrameError)
    assert _warning_messages(caplog) == []


def test_frame_data_is_masked_to_the_frame_length(harness, caplog):
    """Stray bits above the declared frame length belong to neither the frame nor
    the header, so they must be trimmed rather than pushed into `Frame` — which
    would refuse the value and lose the frame.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.handler.handle(_MockMessage(_raw(100, data=0xFFFFFF00, frame_length=16)))
    assert harness.dispatched_counters() == [100]
    assert harness.received[0].request.as_integer == 0xFF00
    assert _warning_messages(caplog) == []


def test_broken_frame_flag_is_preserved(harness):
    """A frame received with a framing error reaches the monitor flagged, not
    dropped.
    """
    harness.handler.handle(_MockMessage(_raw(100, data=0xFF93, frame_length=16, is_broken=True)))
    assert harness.dispatched_counters() == [100]
    assert harness.received[0].request.error is True


def test_callback_exception_does_not_drop_the_rest_of_a_batch(caplog):
    """The guard sits inside the per-slot loop so one failing frame cannot swallow the ones
    behind it. A batch is longer than one slot only when the reorder buffer drains, so feed
    a reorder pattern and fail in the middle of the drained run.
    """
    callbacks = BusTrafficCallbacks(gateway_queue_size=16)
    received: List[BusTrafficItem] = []

    def failing_on_one(item: BusTrafficItem) -> None:
        received.append(item)
        if item.frame_counter == 102:
            raise RuntimeError("subscriber blew up on fc=102")

    callbacks.register(failing_on_one)
    handler = BusMonitorFrameHandler(callbacks, logging.getLogger(_LOGGER_NAME), dev_inst_map=None)

    with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
        # 101 arrives last, so 101/102/103 are published as one drained batch.
        for fc in (100, 102, 103, 101, 104):
            handler.handle(_MockMessage(_raw(fc)))
    assert [item.frame_counter for item in received] == [100, 101, 102, 103, 104]
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


def test_broken_slot_in_reorder_buffer_does_not_wedge_handler(harness, caplog):
    """Regression for the field failure (SOFT-7351): a valid and an unparsable slot both sit
    in the reorder buffer, then a frame past the window flushes it. The old handler raised
    mid-flush and from then on replayed the same buffered frame while dropping every new one.
    """
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        harness.feed(218)
        harness.handler.handle(_MockMessage(_FIELD_INVALID_SLOT))  # fc 220, length 255
        harness.handler.handle(_MockMessage(_raw(221, frame_length=0)))
        harness.feed(225, 226)
    # 220 and 221 are dropped, everything valid goes out exactly once and in order.
    assert harness.dispatched_counters() == [218, 225, 226]
    warnings = _warning_messages(caplog)
    assert any("from 218 to 220" in w for w in warnings)
    assert any("from 221 to 225" in w for w in warnings)
