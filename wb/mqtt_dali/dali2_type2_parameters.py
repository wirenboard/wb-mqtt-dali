# Type 2 absolute input devices

# pylint: disable=duplicate-code

from typing import List

from dali.address import InstanceNumber

from .dali2_parameters import InstanceParam
from .device.absolute_input_device import (
    QueryDeadtimeTimer,
    QueryReportTimer,
    SetDeadtimeTimer,
    SetReportTimer,
)
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
        self.multiplier = 50  # IEC 62386-302 Table: T_incr = 50 ms
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
        # IEC 62386-302 Table: T_incr = 1 s, raw value = seconds directly


def build_type2_absolute_input_device_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        DeadtimeTimerParam(instance_number),
        ReportTimerParam(instance_number),
    ]
