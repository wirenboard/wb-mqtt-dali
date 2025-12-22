# Type 6 LED modules

from dali.address import GearShort
from dali.gear.led import (
    QueryDimmingCurve,
    QueryFastFadeTime,
    QueryMinFastFadeTime,
    SelectDimmingCurve,
    StoreDTRAsFastFadeTime,
)

from .extended_gear_parameters import GearParam, TypeParameters
from .wbdali import WBDALIDriver, query_request


class DimmingCurveParam(GearParam):
    name = "Dimming curve"
    property_name = "type_6_dimming_curve"
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


class FastFadeTimeParam(GearParam):
    name = "Fast fade time"
    property_name = "type_6_fast_fade_time"
    query_command_class = QueryFastFadeTime
    set_command_class = StoreDTRAsFastFadeTime

    async def get_schema(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        try:
            min_time = await query_request(driver, QueryMinFastFadeTime(addr))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read min fast fade time: {e}") from e
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 0,
                    "maximum": min_time,
                }
            },
            "translations": {"ru": {self.name: "Время быстрого затухания"}},
        }


class Type6Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, addr: GearShort) -> list:
        return [
            DimmingCurveParam(),
            FastFadeTimeParam(),
        ]
