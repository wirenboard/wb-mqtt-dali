# Type 17 Dimming curve selection

from dali.address import GearShort

from .extended_gear_parameters import (
    GearParamBase,
    GearParamName,
    NumberGearParam,
    TypeParameters,
)
from .gear.dimming_curve import QueryDimmingCurve, SelectDimmingCurve
from .wbdali import WBDALIDriver


class DimmingCurveParam(NumberGearParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__(GearParamName("Dimming curve"), "type_17_dimming_curve")

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


class Type17Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        return [
            DimmingCurveParam(),
        ]
