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
            SettingsParamName("Deadtime timer"),
            "deadtime_timer",
            instance_number,
            QueryDeadtimeTimer,
            SetDeadtimeTimer,
        )
        self.grid_columns = 4
        self.property_order = 10


class HoldTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Hold timer"),
            "hold_timer",
            instance_number,
            QueryHoldTimer,
            SetHoldTimer,
        )
        self.grid_columns = 4
        self.property_order = 11


class ReportTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Report timer"),
            "report_timer",
            instance_number,
            QueryReportTimer,
            SetReportTimer,
        )
        self.grid_columns = 4
        self.property_order = 12


def build_type3_occupancy_sensor_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        DeadtimeTimerParam(instance_number),
        HoldTimerParam(instance_number),
        ReportTimerParam(instance_number),
    ]
