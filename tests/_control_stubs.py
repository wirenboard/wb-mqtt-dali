"""Stand-in controls for tests about the poll rotation rather than about decoding.

`ReadableControl` sends a recognisable query and throws the answer away, so a test can assert
*which* control was polled *when* without an event and a formatter for it.
"""

from typing import Callable, Optional

from dali.address import Address
from dali.command import Command, Response

from wb.mqtt_dali.common_dali_device import (
    EVENT_RESYNC_BASE_INTERVAL,
    SingleQueryControl,
)
from wb.mqtt_dali.device_publisher import ControlInfo
from wb.mqtt_dali.events import BusEvent


class ReadableControl(SingleQueryControl):
    """Readable pollable whose query defaults to a marker naming the control.

    `decoded_responses` keeps what the transport handed to `decode_response`, `None` for a
    read it rejected.
    """

    def __init__(
        self,
        control_info: ControlInfo,
        query_builder: Optional[Callable[[Address], Command]] = None,
        poll_interval: float = EVENT_RESYNC_BASE_INTERVAL,
    ) -> None:
        super().__init__(
            control_info,
            query_builder=query_builder or (lambda _address: f"Q_{control_info.id}"),
            poll_interval=poll_interval,
        )
        self.decoded_responses: list[Optional[Response]] = []

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        self.decoded_responses.append(response)
        return BusEvent()  # a bare marker: no control's notify reacts to it
