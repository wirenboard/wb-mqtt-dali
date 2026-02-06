# Type 4 light sensor

from typing import List

from dali.device.light import (
    QueryDeadtimeTimer,
    QueryHysteresis,
    QueryHysteresisMin,
    QueryReportTimer,
    SetDeadtimeTimer,
    SetHysteresis,
    SetHysteresisMin,
    SetReportTimer,
)

from .device_parameters import InstanceParam


class DeadtimeTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Deadtime timer", "deadtime_timer", QueryDeadtimeTimer, SetDeadtimeTimer)


class ReportTimerParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Report timer", "report_timer", QueryReportTimer, SetReportTimer)


class HysteresisParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Hysteresis", "hysteresis", QueryHysteresis, SetHysteresis)


class HysteresisMinParam(InstanceParam):
    def __init__(self) -> None:
        super().__init__("Hysteresis minimum", "hysteresis_min", QueryHysteresisMin, SetHysteresisMin)


def build_type4_light_sensor_parameters() -> List[InstanceParam]:
    return [
        DeadtimeTimerParam(),
        ReportTimerParam(),
        HysteresisParam(),
        HysteresisMinParam(),
    ]
