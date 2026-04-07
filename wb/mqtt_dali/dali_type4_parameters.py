# Type 4 Supply voltage controller for incandescent lamps

import logging
from typing import Optional

from dali.address import GearShort
from dali.gear.incandescent import QueryDimmingCurve, SelectDimmingCurve

from .dali_dimming_curve import DimmingCurveState
from .dali_parameters import DimmingCurveParam, TypeParameters
from .wbdali_utils import WBDALIDriver


class Type4DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve


class Type4Parameters(TypeParameters):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__()
        self._dimming_curve_parameter = Type4DimmingCurveParam(dimming_curve_state)
        self._parameters = [self._dimming_curve_parameter]

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        await self._dimming_curve_parameter.read(driver, short_address, logger)
