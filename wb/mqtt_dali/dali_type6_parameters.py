# Type 6 LED modules

from dali.gear.led import (
    QueryDimmingCurve,
    QueryFastFadeTime,
    SelectDimmingCurve,
    StoreDTRAsFastFadeTime,
)

from .dali_dimming_curve import DimmingCurveState
from .dali_parameters import DimmingCurveParam, NumberGearParam, TypeParameters
from .settings import SettingsParamName
from .wbdali_utils import WBDALIDriver


class Type6DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__(dimming_curve_state)


class FastFadeTimeParam(NumberGearParam):
    query_command_class = QueryFastFadeTime
    set_command_class = StoreDTRAsFastFadeTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Fast fade time", "Время быстрого затухания"), "type_6_fast_fade_time"
        )
        self.minimum = 0
        self.maximum = 27


class Type6Parameters(TypeParameters):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__()
        self._dimming_curve_parameter = Type6DimmingCurveParam(dimming_curve_state)
        self._parameters = [
            self._dimming_curve_parameter,
            FastFadeTimeParam(),
        ]

    async def read_mandatory_info(self, driver: WBDALIDriver, short_address: int) -> None:
        await self._dimming_curve_parameter.read(driver, short_address)
