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

    def test_in_order_item_drains_entire_buffer(self):
        # When an in-order item arrives, the entire buffer is flushed unconditionally.
        # [0, 3, 4, 1] → delivered as [0, 1, 3, 4]; cursor advances to 4.
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 3)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 4)
        assert len(received) == 1  # only seq=0 delivered
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 1)
        assert [item.frame_counter for item in received] == [0, 1, 3, 4]


class TestNotifyCommandOverflow:
    def test_overflow_flushes_pending_then_delivers_current(self):
        # After seq=0, buffer [2, 3], then overflow at seq=QUEUE_SIZE
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 2)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 3)
        assert len(received) == 1
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, QUEUE_SIZE)
        assert len(received) == 4
        assert [item.frame_counter for item in received] == [0, 2, 3, QUEUE_SIZE]

    def test_after_overflow_ordering_continues_from_new_cursor(self):
        callbacks = BusTrafficCallbacks(QUEUE_SIZE)
        received = []
        callbacks.register(received.append)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, 0)
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, QUEUE_SIZE)  # overflow
        # Next in-order item after overflow
        callbacks.notify_command(REQUEST_FRAME, None, BusTrafficSource.WB, QUEUE_SIZE + 1)
        assert [item.frame_counter for item in received] == [0, QUEUE_SIZE, QUEUE_SIZE + 1]
