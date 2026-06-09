from dali.frame import BackwardFrame, ForwardFrame

from wb.mqtt_dali.bus_traffic import BusTrafficCallbacks, BusTrafficSource

QUEUE_SIZE = 5
REQUEST_FRAME = ForwardFrame(16, 0xFE00)
RESPONSE_FRAME = BackwardFrame(0x42)


class TestRegistration:
    def test_register_returns_working_cleanup(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        cleanup = callbacks.register(received.append)
        callbacks.notify_bus_frame(REQUEST_FRAME, 0)
        assert len(received) == 1
        cleanup()
        callbacks.notify_bus_frame(REQUEST_FRAME, 1)
        assert len(received) == 1  # not called after cleanup

    def test_multiple_callbacks_all_receive(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received_a, received_b = [], []
        callbacks.register(received_a.append)
        callbacks.register(received_b.append)
        callbacks.notify_bus_frame(REQUEST_FRAME, 0)
        assert len(received_a) == 1
        assert len(received_b) == 1

    def test_no_callbacks_no_error(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        callbacks.notify_bus_frame(REQUEST_FRAME, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)


class TestNotifyBusFrame:
    def test_delivered_immediately(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_bus_frame(REQUEST_FRAME, 42)
        assert len(received) == 1

    def test_item_fields(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_bus_frame(REQUEST_FRAME, 42)
        item = received[0]
        assert item.request == REQUEST_FRAME
        assert item.response is None
        assert item.request_source == BusTrafficSource.BUS
        assert item.frame_counter == 42

    def test_bus_frames_bypass_command_ordering(self):
        # BUS frames must be delivered even when WB commands are buffered
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 1)  # buffered
        callbacks.notify_bus_frame(REQUEST_FRAME, 99)
        assert len(received) == 1
        assert received[0].request_source == BusTrafficSource.BUS


class TestNotifyCommandInOrder:
    def test_first_item_delivered_immediately(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, RESPONSE_FRAME, BusTrafficSource.WB, 0)
        assert len(received) == 1

    def test_item_fields(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, RESPONSE_FRAME, BusTrafficSource.LUNATONE, 0)
        item = received[0]
        assert item.request == REQUEST_FRAME
        assert item.response == RESPONSE_FRAME
        assert item.request_source == BusTrafficSource.LUNATONE
        assert item.frame_counter == 0

    def test_sequential_items_delivered_in_order(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        for seq in range(4):
            callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, seq)
        assert [item.frame_counter for item in received] == [0, 1, 2, 3]


class TestNotifyCommandOutOfOrder:
    def test_late_item_buffered_until_gap_filled(self):
        # [0, 2, 1] → delivered as [0, 1, 2]
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 2)
        assert len(received) == 1  # seq=2 is buffered
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 1)
        assert [item.frame_counter for item in received] == [0, 1, 2]

    def test_reversed_pair_delivered_in_order(self):
        # [1, 0] → delivered as [0, 1]
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 1)
        assert len(received) == 0  # seq=1 buffered, waiting for seq=0
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        assert [item.frame_counter for item in received] == [0, 1]

    def test_in_order_item_drains_only_contiguous_prefix(self):
        # When an in-order item fills a gap, only items contiguous with the
        # advancing cursor are flushed; items past a further gap stay buffered.
        # [0, 3, 4, 1] → delivered as [0, 1]; 3, 4 wait for seq=2.
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 3)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 4)
        assert len(received) == 1  # only seq=0 delivered
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 1)
        assert [item.frame_counter for item in received] == [0, 1]


class TestNotifyCommandGapPreservation:
    def test_noncontiguous_buffer_flushed_only_when_gap_filled(self):
        """0,2,4 arrive (2,4 buffered behind the gap at 1); when 1 fills the gap the
        contiguous prefix 1,2 is published while 4 keeps waiting behind the gap at 3;
        when 3 finally arrives, 3,4 are published in order. No frame is published
        ahead of its predecessor.
        """
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 2)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 4)
        assert [item.frame_counter for item in received] == [0]
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 1)
        assert [item.frame_counter for item in received] == [0, 1, 2]  # 4 still buffered
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 3)
        assert [item.frame_counter for item in received] == [0, 1, 2, 3, 4]

    def test_no_frame_dropped_across_gap(self):
        """A frame landing in a gap (seq=3) must eventually be delivered rather than
        silently dropped: after 0,2,4 then 1 advance the cursor only to 1, the later
        3 fills the remaining gap and every sequence 0..4 is delivered exactly once.
        """
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        for seq in (0, 2, 4, 1, 3):
            callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, seq)
        assert sorted(item.frame_counter for item in received) == [0, 1, 2, 3, 4]
        assert len(received) == 5  # nothing dropped, nothing duplicated

    def test_in_order_delivery_unchanged(self):
        """A strictly increasing sequence is published immediately and in order
        (regression guard for the unchanged fast path).
        """
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        for seq in range(5):
            callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, seq)
            assert len(received) == seq + 1  # each item delivered immediately
        assert [item.frame_counter for item in received] == [0, 1, 2, 3, 4]


class TestNotifyCommandRingWrap:
    def test_full_ring_wrap_delivers_in_order_without_loss(self):
        """The device ring buffer is read by cell index, not by sequence_id, so when the
        ring wraps the lowest sequence_id (sitting in the highest cell) is read last
        while the larger ones are read first. With QUEUE_SIZE=5 the cursor sits at 0 and
        the window 1..5 arrives as 2,3,4,5,1 — the wrapped frame 1 trailing by the full
        queue depth. The frame at distance exactly QUEUE_SIZE (seq=5) is a legitimately
        reordered frame, not an unrecoverable gap, so it must stay buffered; when 1
        finally arrives every frame 1..5 is delivered in order and none is dropped.
        """
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        for seq in (2, 3, 4, 5, 1):
            callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, seq)
        assert [item.frame_counter for item in received] == [0, 1, 2, 3, 4, 5]

    def test_stale_duplicate_does_not_block_drain(self):
        """A stale frame whose sequence_id is at or below the cursor must not wedge the
        buffer head and stall delivery of genuinely contiguous frames. The cursor is
        driven to 5, a stale seq=2 is injected (lands at the head as the smallest), then
        7 is buffered behind the gap at 6; when 6 arrives the stale 2 is discarded rather
        than blocking the drain, so 6 and 7 are still delivered and 2 is not republished.
        """
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        for seq in range(6):
            callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, seq)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 2)  # stale duplicate
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 7)  # buffered behind gap at 6
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 6)
        assert [item.frame_counter for item in received] == [0, 1, 2, 3, 4, 5, 6, 7]


class TestNotifyCommandOverflow:
    def test_overflow_flushes_pending_then_delivers_current(self):
        # After seq=0, buffer [2, 3], then overflow past the reorder window at seq=QUEUE_SIZE+1
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 2)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 3)
        assert len(received) == 1
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, QUEUE_SIZE + 1)
        assert len(received) == 4
        assert [item.frame_counter for item in received] == [0, 2, 3, QUEUE_SIZE + 1]

    def test_overflow_flush_skips_stale_duplicate(self):
        """A stale duplicate buffered at or below the cursor must not be republished when a
        later large gap forces an overflow flush; it is discarded, as in the normal drain.
        The cursor is driven to 5, a stale seq=2 is buffered, then a gap past the reorder
        window flushes the buffer — the stale 2 is dropped, not re-emitted.
        """
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        for seq in range(6):
            callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, seq)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 2)  # stale, buffered
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 5 + QUEUE_SIZE + 1)  # overflow
        assert [item.frame_counter for item in received] == [0, 1, 2, 3, 4, 5, 5 + QUEUE_SIZE + 1]

    def test_after_overflow_ordering_continues_from_new_cursor(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, QUEUE_SIZE)  # overflow
        # Next in-order item after overflow
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, QUEUE_SIZE + 1)
        assert [item.frame_counter for item in received] == [0, QUEUE_SIZE, QUEUE_SIZE + 1]
