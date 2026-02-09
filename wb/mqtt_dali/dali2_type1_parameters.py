# Type 1 push buttons

from typing import List

from dali.address import InstanceNumber
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

from .dali2_parameters import InstanceParam
from .settings import SettingsParamName


class DoubleTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Double timer"),
            "double_timer",
            instance_number,
            QueryDoubleTimer,
            SetDoubleTimer,
        )


class ReportTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Report timer"),
            "report_timer",
            instance_number,
            QueryRepeatTimer,
            SetRepeatTimer,
        )


class ShortTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Short timer"),
            "short_timer",
            instance_number,
            QueryShortTimer,
            SetShortTimer,
        )


class StuckTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Stuck timer"),
            "stuck_timer",
            instance_number,
            QueryStuckTimer,
            SetStuckTimer,
        )


def build_type1_push_button_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        DoubleTimerParam(instance_number),
        ReportTimerParam(instance_number),
        ShortTimerParam(instance_number),
        StuckTimerParam(instance_number),
    ]
