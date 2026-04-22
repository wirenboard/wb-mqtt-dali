# Type 17 Dimming curve selection


import logging
from typing import Optional

from dali.address import GearShort

from .dali_dimming_curve import DimmingCurveState
from .dali_parameters import DimmingCurveParam, TypeParameters
from .gear.dimming_curve import QueryDimmingCurve, SelectDimmingCurve
from .wbdali import WBDALIDriver


class Type17DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve


class Type17Parameters(TypeParameters):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__()
        self._dimming_curve_parameter = Type17DimmingCurveParam(dimming_curve_state)
        self._parameters = [self._dimming_curve_parameter]

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        await self._dimming_curve_parameter.read(driver, short_address, logger)
