# Type 6 LED modules

from dali.address import GearShort
from dali.gear.led import (
    QueryDimmingCurve,
    QueryFastFadeTime,
    QueryMinFastFadeTime,
    SelectDimmingCurve,
    StoreDTRAsFastFadeTime,
)

from .dali_parameters import DimmingCurveParam, NumberGearParam, TypeParameters
from .settings import SettingsParamName
from .wbdali_utils import WBDALIDriver, query_int


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

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        res = await super().read(driver, short_address)
        try:
            self.maximum = await query_int(driver, QueryMinFastFadeTime(GearShort(short_address)))
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
