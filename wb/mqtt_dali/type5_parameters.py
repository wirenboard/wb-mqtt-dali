# Type 5 Conversion from digital signal into d. c. voltage

from dali.address import GearShort
from dali.gear.converter import (
    QueryConverterFeatures,
    QueryDimmingCurve,
    SelectDimmingCurve,
)

from .extended_gear_parameters import (
    GearParamBase,
    GearParamName,
    NumberGearParam,
    TypeParameters,
)
from .wbdali import WBDALIDriver, query_request

# TODO: Output range is write only


class DimmingCurveParam(NumberGearParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__(GearParamName("Dimming curve"), "type_5_dimming_curve")

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name.en,
                    "type": "integer",
                    "enum": [0, 1],
                    "options": {"enum_titles": ["standard", "linear"]},
                }
            },
            "translations": {
                "ru": {
                    self.name.en: "Кривая диммирования",
                    "standard": "стандартная",
                    "linear": "линейная",
                }
            },
        }


class Type5Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        try:
            features = await query_request(driver, QueryConverterFeatures(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read converter features: {e}") from e
        if not ((features >> 5) & 1):  # 5th bit: dimming curve selectable
            return []
        return [DimmingCurveParam()]
