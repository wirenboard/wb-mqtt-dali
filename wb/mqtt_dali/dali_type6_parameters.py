# Type 6 LED modules

import logging
from typing import Optional

from dali.address import GearShort
from dali.gear.led import (
    QueryDimmingCurve,
    QueryFastFadeTime,
    SelectDimmingCurve,
    StoreDTRAsFastFadeTime,
)

from .dali_dimming_curve import DimmingCurveState
from .dali_parameters import DimmingCurveParam, NumberGearParam, TypeParameters
from .settings import SettingsParamName
from .utils import add_enum
from .wbdali_utils import WBDALIDriver


class Type6DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve


class FastFadeTimeParam(NumberGearParam):
    query_command_class = QueryFastFadeTime
    set_command_class = StoreDTRAsFastFadeTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Fast fade time, ms", "Время быстрого изменения, ms"), "type_6_fast_fade_time"
        )
        self.minimum = 0
        self.maximum = 27
        self.grid_columns = 6

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(group_and_broadcast)
        add_enum(
            schema["properties"][self.property_name],
            [
                (0, "0"),
                (1, "100 (1)"),
                (2, "100 (2)"),
                (3, "100 (3)"),
                (4, "100 (4)"),
                (5, "100 (5)"),
                (6, "200 (6)"),
                (7, "200 (7)"),
                (8, "200 (8)"),
                (9, "200 (9)"),
                (10, "300 (10)"),
                (11, "300 (11)"),
                (12, "300 (12)"),
                (13, "300 (13)"),
                (14, "400 (14)"),
                (15, "400 (15)"),
                (16, "400 (16)"),
                (17, "400 (17)"),
                (18, "500 (18)"),
                (19, "500 (19)"),
                (20, "500 (20)"),
                (21, "500 (21)"),
                (22, "600 (22)"),
                (23, "600 (23)"),
                (24, "600 (24)"),
                (25, "600 (25)"),
                (26, "700 (26)"),
                (27, "700 (27)"),
            ],
        )
        return schema


class Type6Parameters(TypeParameters):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__()
        self._dimming_curve_parameter = Type6DimmingCurveParam(dimming_curve_state)
        self._dimming_curve_parameter.grid_columns = 6
        self._parameters = [
            self._dimming_curve_parameter,
            FastFadeTimeParam(),
        ]

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        await self._dimming_curve_parameter.read(driver, short_address, logger)
