# Type 6 general purpose sensor

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
            SettingsParamName("Alarm report timer", "Таймер аварийного отчёта"),
            "alarm_report_timer",
            instance_number,
            QueryAlarmReportTimer,
            SetAlarmReportTimer,
        )
        self.grid_columns = 3
        self.property_order = 10


class DeadtimeTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Deadtime timer", "Таймер задержки"),
            "deadtime_timer",
            instance_number,
            QueryDeadtimeTimer,
            SetDeadtimeTimer,
        )
        self.grid_columns = 3
        self.property_order = 11


class HysteresisParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Hysteresis", "Гистерезис"),
            "hysteresis",
            instance_number,
            QueryHysteresis,
            SetHysteresis,
        )
        self.grid_columns = 3
        self.property_order = 12


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
            SettingsParamName("Report timer", "Таймер отчёта"),
            "report_timer",
            instance_number,
            QueryReportTimer,
            SetReportTimer,
        )
        self.grid_columns = 3
        self.property_order = 14


def build_type6_general_purpose_sensor_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        AlarmReportTimerParam(instance_number),
        DeadtimeTimerParam(instance_number),
        HysteresisParam(instance_number),
        HysteresisMinParam(instance_number),
        ReportTimerParam(instance_number),
    ]
