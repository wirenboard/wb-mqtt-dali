from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from dali.frame import Frame


class BusTrafficSource(Enum):
    BUS = "bus"
    WB = "wb"
    LUNATONE = "lunatone-iot-emulator"


@dataclass
class BusTrafficItem:
    # Request raw frame object
    request: Frame

    # Matching response raw frame object
    response: Optional[Frame]

    # Identifier of the request frame source
    request_source: BusTrafficSource

    # Frame counter for tracking and logging purposes.
    # For BUS-sourced frames: hardware bus monitor counter.
    # For WB/LUNATONE-sourced frames: sequence_id assigned when placed in the gateway send queue.
    frame_counter: int


class BusTrafficCallbacks:
    """Helper class for callback registration"""

    def __init__(self, gateway_queue_size: int) -> None:
        self._callbacks = set()
        self._gateway_queue_size = gateway_queue_size
        self._last_item_sequence_id = -1
        self._waiting_for_publish: list[BusTrafficItem] = []

    def register(self, func: Callable[[BusTrafficItem], None]) -> Callable[[], None]:
        def cleanup():
            self._callbacks.discard(func)

        self._callbacks.add(func)
        return cleanup

    def notify_bus_frame(self, frame: Frame, frame_counter: int) -> None:
        """
        Deliver an unexpected frame observed on the bus (hardware bus monitor).
        Delivered immediately to all callbacks without buffering or reordering.
        """
        self._dispatch(BusTrafficItem(frame, None, BusTrafficSource.BUS, frame_counter))

    def notify_command(
        self,
        request: Frame,
        response: Optional[Frame],
        source: BusTrafficSource,
        sequence_id: int,
    ) -> None:
        """
        Deliver a command sent by the service (WB or Lunatone) together with its response.

        Commands carry a sequence_id assigned when placed in the gateway send queue.
        Responses may arrive out of order relative to each other, so items are buffered
        and drained in sequence_id order
        """
        item = BusTrafficItem(request, response, source, sequence_id)
        if sequence_id == self._last_item_sequence_id + 1:
            self._dispatch(item)
            self._last_item_sequence_id = sequence_id
            for pending_item in self._waiting_for_publish:
                self._dispatch(pending_item)
                self._last_item_sequence_id = pending_item.frame_counter
            self._waiting_for_publish.clear()
            return
        if sequence_id >= self._last_item_sequence_id + self._gateway_queue_size:
            # Gap is too large to wait out — flush buffered items in their sorted order
            for pending_item in self._waiting_for_publish:
                self._dispatch(pending_item)
            self._waiting_for_publish.clear()
            self._dispatch(item)
            self._last_item_sequence_id = sequence_id
            return
        self._waiting_for_publish.append(item)
        self._waiting_for_publish.sort(key=lambda i: i.frame_counter or 0)

    def _dispatch(self, item: BusTrafficItem) -> None:
        for func in self._callbacks:
            func(item)
