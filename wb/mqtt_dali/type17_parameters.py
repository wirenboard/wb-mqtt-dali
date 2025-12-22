# Type 17 Dimming curve selection

from dali.address import GearShort

from .extended_gear_parameters import GearParam, TypeParameters
from .gear.dimming_curve import QueryDimmingCurve, SelectDimmingCurve
from .wbdali import WBDALIDriver


class DimmingCurveParam(GearParam):
    name = "Dimming curve"
    property_name = "type_17_dimming_curve"
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    async def get_schema(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "enum": [0, 1],
                    "options": {"enum_titles": ["standard", "linear"]},
                }
            },
            "translations": {
                "ru": {
                    self.name: "Кривая диммирования",
                    "standard": "стандартная",
                    "linear": "линейная",
                }
            },
        }


class Type17Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, addr: GearShort) -> list:
        return [
            DimmingCurveParam(),
        ]
