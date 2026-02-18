# Type 7 Switching function

from dali.address import GearShort

from .dali_parameters import NumberGearParam, TypeParameters
from .gear.switching_function import (
    QueryDownSwitchOffThreshold,
    QueryDownSwitchOnThreshold,
    QueryErrorHoldOffTime,
    QueryFeatures,
    QueryUpSwitchOffThreshold,
    QueryUpSwitchOnThreshold,
    StoreDTRAsDownSwitchOffThreshold,
    StoreDTRAsDownSwitchOnThreshold,
    StoreDTRAsErrorHoldOffTime,
    StoreDTRAsUpSwitchOffThreshold,
    StoreDTRAsUpSwitchOnThreshold,
)
from .settings import SettingsParamName
from .wbdali_utils import WBDALIDriver, query_request


class UpSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOnThreshold
    set_command_class = StoreDTRAsUpSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Up switch on threshold", "Верхний порог включения переключателя"),
            "type_7_up_switch_on_threshold",
        )
        self.minimum = 1


class UpSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryUpSwitchOffThreshold
    set_command_class = StoreDTRAsUpSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Up switch off threshold", "Верхний порог выключения переключателя"),
            "type_7_up_switch_off_threshold",
        )
        self.minimum = 1


class DownSwitchOnThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOnThreshold
    set_command_class = StoreDTRAsDownSwitchOnThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Down switch on threshold", "Нижний порог включения переключателя"),
            "type_7_down_switch_on_threshold",
        )


class DownSwitchOffThresholdParam(NumberGearParam):
    query_command_class = QueryDownSwitchOffThreshold
    set_command_class = StoreDTRAsDownSwitchOffThreshold

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Down switch off threshold", "Нижний порог выключения переключателя"),
            "type_7_down_switch_off_threshold",
        )


class ErrorHoldOffTimeParam(NumberGearParam):
    query_command_class = QueryErrorHoldOffTime
    set_command_class = StoreDTRAsErrorHoldOffTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Error holdoff time", "Время задержки ошибки"), "type_7_error_holdoff_time"
        )


class Type7Parameters(TypeParameters):
    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        try:
            features = await query_request(driver, QueryFeatures(GearShort(short_address)))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read switching function features: {e}") from e
        res = []
        if (features >> 3) & 1:  # bit 3: adjustable thresholds
            res.append(UpSwitchOnThresholdParam())
            res.append(UpSwitchOffThresholdParam())
            res.append(DownSwitchOnThresholdParam())
            res.append(DownSwitchOffThresholdParam())
        if (features >> 4) & 1:  # bit 4: adjustable hold-off time
            res.append(ErrorHoldOffTimeParam())
        self._parameters = res
        return await super().read(driver, short_address)
