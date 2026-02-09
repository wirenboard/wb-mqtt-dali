# Type 1 push buttons

from typing import List

from dali.device.pushbutton import (
    QueryDoubleTimer,
    QueryRepeatTimer,
    QueryShortTimer,
    QueryStuckTimer,
    SetDoubleTimer,
    SetRepeatTimer,
    SetShortTimer,
    SetStuckTimer,
)

from .device_parameters import InstanceParam


class DoubleTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Double timer", "double_timer", QueryDoubleTimer, SetDoubleTimer)


class ReportTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Report timer", "report_timer", QueryRepeatTimer, SetRepeatTimer)


class ShortTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Short timer", "short_timer", QueryShortTimer, SetShortTimer)


class StuckTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Stuck timer", "stuck_timer", QueryStuckTimer, SetStuckTimer)


def build_type1_push_button_parameters() -> List[InstanceParam]:
    return [
        DoubleTimerParam(),
        ReportTimerParam(),
        ShortTimerParam(),
        StuckTimerParam(),
    ]
