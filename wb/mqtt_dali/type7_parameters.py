# Type 7 Switching function

from dali.address import GearShort

from .extended_gear_parameters import (
    GearParamBase,
    GearParamName,
    NumberGearParam,
    TypeParameters,
)
from .gear.switching_function import (
    QueryDownSwitchOffThreshold,
    QueryDownSwitchOnThreshold,
    QueryErrorHoldOffTime,
    QueryUpSwitchOffThreshold,
    QueryUpSwitchOnThreshold,
    StoreDTRAsDownSwitchOffThreshold,
    StoreDTRAsDownSwitchOnThreshold,
    StoreDTRAsErrorHoldOffTime,
    StoreDTRAsUpSwitchOffThreshold,
    StoreDTRAsUpSwitchOnThreshold,
)
from .wbdali import WBDALIDriver


class UpSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOnThreshold
    set_command_class = StoreDTRAsUpSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Up switch on threshold", "Верхний порог включения переключателя"),
            "type_7_up_switch_on_threshold",
        )
        self.minimum = 1


class UpSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOffThreshold
    set_command_class = StoreDTRAsUpSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Up switch off threshold", "Верхний порог выключения переключателя"),
            "type_7_up_switch_off_threshold",
        )
        self.minimum = 1


class DownSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOnThreshold
    set_command_class = StoreDTRAsDownSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Down switch on threshold", "Нижний порог включения переключателя"),
            "type_7_down_switch_on_threshold",
        )


class DownSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOffThreshold
    set_command_class = StoreDTRAsDownSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Down switch off threshold", "Нижний порог выключения переключателя"),
            "type_7_down_switch_off_threshold",
        )


class ErrorHoldOffTimeParam(NumberGearParam):
    query_command_class = QueryErrorHoldOffTime
    set_command_class = StoreDTRAsErrorHoldOffTime

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Error holdoff time", "Время задержки ошибки"), "type_7_error_holdoff_time"
        )


class Type7Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        return [
            UpSwitchOnThresholdParam(),
            UpSwitchOffThresholdParam(),
            DownSwitchOnThresholdParam(),
            DownSwitchOffThresholdParam(),
            ErrorHoldOffTimeParam(),
        ]
