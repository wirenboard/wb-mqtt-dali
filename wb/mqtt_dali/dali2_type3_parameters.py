# Type 3 occupancy sensor

from typing import List

from dali.device.occupancy import (
    QueryDeadtimeTimer,
    QueryHoldTimer,
    QueryReportTimer,
    SetDeadtimeTimer,
    SetHoldTimer,
    SetReportTimer,
)

from .dali_2_device import InstanceParam


class DeadtimeTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Deadtime timer", "deadtime_timer", QueryDeadtimeTimer, SetDeadtimeTimer)


class HoldTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Hold timer", "hold_timer", QueryHoldTimer, SetHoldTimer)


class ReportTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Report timer", "report_timer", QueryReportTimer, SetReportTimer)


def build_type3_occupancy_sensor_parameters() -> List[InstanceParam]:
    return [
        DeadtimeTimerParam(),
        HoldTimerParam(),
        ReportTimerParam(),
    ]
