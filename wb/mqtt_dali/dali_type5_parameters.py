# Type 5 Conversion from digital signal into d. c. voltage

from dali.address import GearShort
from dali.gear.converter import (
    QueryConverterFeatures,
    QueryDimmingCurve,
    SelectDimmingCurve,
)

from .dali_parameters import DimmingCurveParam, TypeParameters
from .wbdali import WBDALIDriver, query_request

# TODO: Output range is write only


class Type5DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__("type_5_dimming_curve")


class Type5Parameters(TypeParameters):
    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        try:
            features = await query_request(driver, QueryConverterFeatures(GearShort(short_address)))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read converter features: {e}") from e
        if not ((features >> 5) & 1):  # 5th bit: dimming curve selectable
            return {}
        self._parameters = [Type5DimmingCurveParam()]
        return await super().read(driver, short_address)
