# Type 4 Supply voltage controller for incandescent lamps

from dali.address import GearShort
from dali.gear.incandescent import QueryDimmingCurve, SelectDimmingCurve

from .extended_gear_parameters import (
    GearParamBase,
    GearParamName,
    NumberGearParam,
    TypeParameters,
)
from .wbdali import WBDALIDriver


class DimmingCurveParam(NumberGearParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__(GearParamName("Dimming curve"), "type_4_dimming_curve")

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


class Type4Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        return [DimmingCurveParam()]
