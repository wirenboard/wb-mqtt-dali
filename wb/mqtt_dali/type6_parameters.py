# Type 6 LED modules

from dali.gear.led import (
    QueryDimmingCurve,
    QueryFastFadeTime,
    QueryMinFastFadeTime,
    SelectDimmingCurve,
    StoreDTRAsFastFadeTime,
)

from .extended_gear_parameters import DimmingCurveParam, NumberGearParam, TypeParameters
from .settings import SettingsParamAddress, SettingsParamName
from .wbdali import WBDALIDriver, query_request


class Type6DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__("type_6_dimming_curve")


class FastFadeTimeParam(NumberGearParam):
    query_command_class = QueryFastFadeTime
    set_command_class = StoreDTRAsFastFadeTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Fast fade time", "Время быстрого затухания"), "type_6_fast_fade_time"
        )

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        res = await super().read(driver, address)
        try:
            self.maximum = await query_request(driver, QueryMinFastFadeTime(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read min fast fade time: {e}") from e
        return res


class Type6Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._parameters = [
            Type6DimmingCurveParam(),
            FastFadeTimeParam(),
        ]
