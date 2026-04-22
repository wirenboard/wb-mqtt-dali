# Type 5 Conversion from digital signal into d. c. voltage

import logging
from typing import Optional

from dali.address import GearShort
from dali.gear.converter import (
    QueryConverterFeatures,
    QueryDimmingCurve,
    SelectDimmingCurve,
)

from .dali_dimming_curve import DimmingCurveState
from .dali_parameters import DimmingCurveParam, TypeParameters
from .wbdali import WBDALIDriver
from .wbdali_utils import query_response


class Type5DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve


class Type5Parameters(TypeParameters):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__()
        self._dimming_curve_state = dimming_curve_state
        self._dimming_curve_parameter = None

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        try:
            features = await query_response(driver, QueryConverterFeatures(short_address), logger=logger)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read converter features: {e}") from e
        if getattr(features, "nonlogarithmic_dimming_curve_supported") is True:
            self._dimming_curve_parameter = Type5DimmingCurveParam(self._dimming_curve_state)
            self._parameters = [self._dimming_curve_parameter]
            await self._dimming_curve_parameter.read(driver, short_address, logger)
        else:
            self._dimming_curve_parameter = None
