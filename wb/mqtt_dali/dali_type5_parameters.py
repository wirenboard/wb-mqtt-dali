# Type 5 Conversion from digital signal into d. c. voltage

from dali.address import GearShort
from dali.gear.converter import (
    QueryConverterFeatures,
    QueryDimmingCurve,
    SelectDimmingCurve,
)

from .dali_dimming_curve import DimmingCurveState
from .dali_parameters import DimmingCurveParam, TypeParameters
from .wbdali_utils import WBDALIDriver, query_response

# TODO: Output range is write only


class Type5DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__(dimming_curve_state)


class Type5Parameters(TypeParameters):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__()
        self._dimming_curve_state = dimming_curve_state
        self._dimming_curve_parameter = None

    async def read_mandatory_info(self, driver: WBDALIDriver, short_address: GearShort) -> None:
        try:
            features = await query_response(driver, QueryConverterFeatures(short_address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read converter features: {e}") from e
        if getattr(features, "nonlogarithmic_dimming_curve_supported") is True:
            self._dimming_curve_parameter = Type5DimmingCurveParam(self._dimming_curve_state)
            self._parameters = [self._dimming_curve_parameter]
            await self._dimming_curve_parameter.read(driver, short_address)
        else:
            self._dimming_curve_parameter = None
