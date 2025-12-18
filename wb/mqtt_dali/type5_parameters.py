# Type 5 Conversion from digital signal into d. c. voltage

from dali.address import GearShort
from dali.gear.converter import (
    QueryConverterFeatures,
    QueryDimmingCurve,
    SelectDimmingCurve,
)

from .extended_gear_parameters import GearParam, TypeParameters
from .wbdali import WBDALIDriver, send_extended_command

# TODO: Output range is write only


class DimmingCurveParam(GearParam):
    name = "Dimming curve"
    property_name = "type_5_dimming_curve"
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


class Type5Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, addr: GearShort) -> list:
        features = await send_extended_command(driver, QueryConverterFeatures(addr))
        if not features or not features.bits[5]:
            return []
        return [DimmingCurveParam()]
