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
            SettingsParamName("Double timer, ms", "Таймер двойного нажатия, мс"),
            "double_timer",
            instance_number,
            QueryDoubleTimer,
            SetDoubleTimer,
        )
        self.property_order = 11
        self.grid_columns = 3
        self.multiplier = 20  # IEC 62386-301 Table 4: T_incr = 20 ms
        self.maximum = 255 * 20


class RepeatTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Repeat timer, ms", "Таймер повтора, мс"),
            "repeat_timer",
            instance_number,
            QueryRepeatTimer,
            SetRepeatTimer,
        )
        self.property_order = 12
        self.grid_columns = 3
        self.multiplier = 20  # IEC 62386-301 Table 4: T_incr = 20 ms
        self.maximum = 255 * 20


class ShortTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Short timer, ms", "Таймер короткого нажатия, мс"),
            "short_timer",
            instance_number,
            QueryShortTimer,
            SetShortTimer,
        )
        self.default = 200  # IEC 62386-301 Table 4, Table 9
        self.property_order = 10
        self.grid_columns = 3
        self.multiplier = 20  # IEC 62386-301 Table 4: T_incr = 20 ms
        self.minimum = 200  # IEC 62386-301 Table 9
        self.maximum = 255 * 20


class StuckTimerParam(InstanceParam):
    def __init__(self, instance_number: InstanceNumber) -> None:
        super().__init__(
            SettingsParamName("Stuck timer, s", "Таймер залипания, с"),
            "stuck_timer",
            instance_number,
            QueryStuckTimer,
            SetStuckTimer,
        )
        self.property_order = 13
        self.grid_columns = 3
        # IEC 62386-301 Table 4: T_incr = 1 s, raw value = seconds directly


def build_type1_push_button_parameters(instance_number: InstanceNumber) -> List[InstanceParam]:
    return [
        DoubleTimerParam(instance_number),
        RepeatTimerParam(instance_number),
        ShortTimerParam(instance_number),
        StuckTimerParam(instance_number),
    ]
