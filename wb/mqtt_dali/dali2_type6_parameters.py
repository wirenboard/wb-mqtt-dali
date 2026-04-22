# Type 6 general purpose sensor

# pylint: disable=duplicate-code

from typing import List

from dali.address import InstanceNumber

from .dali2_parameters import InstanceParam
from .device.general_purpose_sensor import (
    QueryAlarmReportTimer,
    QueryDeadtimeTimer,
    QueryHysteresis,
    QueryHysteresisMin,
    QueryReportTimer,
    SetAlarmReportTimer,
    SetDeadtimeTimer,
    SetHysteresis,
    SetHysteresisMin,
    SetReportTimer,
)
from .settings import SettingsParamName


class AlarmReportTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Alarm report timer, s", "Таймер аварийного отчёта, с"),
            "alarm_report_timer",
            instance_number,
            QueryAlarmReportTimer,
            SetAlarmReportTimer,
        )
        self.grid_columns = 3
        self.property_order = 10
        self.multiplier = 5  # IEC 62386-306 Table 6: T_incr = 5 s
        self.maximum = 255 * 5


class DeadtimeTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Deadtime timer, ms", "Таймер задержки, мс"),
            "deadtime_timer",
            instance_number,
            QueryDeadtimeTimer,
            SetDeadtimeTimer,
        )
        self.grid_columns = 3
        self.property_order = 11
        self.multiplier = 50  # IEC 62386-306 Table 6: T_incr = 50 ms
        self.maximum = 255 * 50


class HysteresisParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Hysteresis, %", "Гистерезис, %"),
            "hysteresis",
            instance_number,
            QueryHysteresis,
            SetHysteresis,
        )
        self.grid_columns = 3
        self.property_order = 12
        self.maximum = 25  # IEC 62386-306: hysteresis range is 0..25 %


class HysteresisMinParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Hysteresis minimum", "Минимальный гистерезис"),
            "hysteresis_min",
            instance_number,
            QueryHysteresisMin,
            SetHysteresisMin,
        )
        self.grid_columns = 3
        self.property_order = 13


class ReportTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Report timer, s", "Таймер отчёта, с"),
            "report_timer",
            instance_number,
            QueryReportTimer,
            SetReportTimer,
        )
        self.grid_columns = 3
        self.property_order = 14
        self.multiplier = 5  # IEC 62386-306 Table 6: T_incr = 5 s
        self.maximum = 255 * 5


def build_type6_general_purpose_sensor_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        AlarmReportTimerParam(instance_number),
        DeadtimeTimerParam(instance_number),
        HysteresisParam(instance_number),
        HysteresisMinParam(instance_number),
        ReportTimerParam(instance_number),
    ]
