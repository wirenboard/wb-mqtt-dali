# Type 4 light sensor

from typing import List

from dali.address import InstanceNumber
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

from .dali2_parameters import InstanceParam
from .settings import SettingsParamName


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
        self.property_order = 10
        self.multiplier = 50  # IEC 62386-304 Table 3: T_incr = 50 ms
        self.maximum = 255 * 50


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
        self.property_order = 11
        # IEC 62386-304 Table 3: T_incr = 1 s, raw value = seconds directly


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
        self.maximum = 25  # IEC 62386-304: hysteresis range is 0..25 %


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
        self.property_order = 14


def build_type4_light_sensor_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        DeadtimeTimerParam(instance_number),
        ReportTimerParam(instance_number),
        HysteresisParam(instance_number),
        HysteresisMinParam(instance_number),
    ]
