# Type 3 occupancy sensor

from typing import List

from dali.address import InstanceNumber
from dali.device.occupancy import (
    QueryDeadtimeTimer,
    QueryHoldTimer,
    QueryReportTimer,
    SetDeadtimeTimer,
    SetHoldTimer,
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
        self.grid_columns = 4
        self.property_order = 10
        self.multiplier = 50  # IEC 62386-303 Table 4: T_incr = 50 ms
        self.maximum = 255 * 50


class HoldTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Hold timer, s", "Таймер удержания, с"),
            "hold_timer",
            instance_number,
            QueryHoldTimer,
            SetHoldTimer,
        )
        self.grid_columns = 4
        self.property_order = 11
        self.multiplier = 10  # IEC 62386-303 Table 4: T_incr = 10 s
        self.maximum = 255 * 10


class ReportTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Report timer, s", "Таймер отчёта, с"),
            "report_timer",
            instance_number,
            QueryReportTimer,
            SetReportTimer,
        )
        self.grid_columns = 4
        self.property_order = 12
        # IEC 62386-303 Table 4: T_incr = 1 s, raw value = seconds directly


def build_type3_occupancy_sensor_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        DeadtimeTimerParam(instance_number),
        HoldTimerParam(instance_number),
        ReportTimerParam(instance_number),
    ]
