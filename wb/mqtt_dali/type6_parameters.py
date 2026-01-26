# Type 6 LED modules

from dali.address import GearShort
from dali.gear.led import (
    QueryDimmingCurve,
    QueryFastFadeTime,
    QueryMinFastFadeTime,
    SelectDimmingCurve,
    StoreDTRAsFastFadeTime,
)

from .extended_gear_parameters import (
    GearParamBase,
    GearParamName,
    NumberGearParam,
    TypeParameters,
)
from .wbdali import WBDALIDriver, query_request


class DimmingCurveParam(NumberGearParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__(GearParamName("Dimming curve"), "type_6_dimming_curve")

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


class FastFadeTimeParam(NumberGearParam):
    query_command_class = QueryFastFadeTime
    set_command_class = StoreDTRAsFastFadeTime

    def __init__(self) -> None:
        super().__init__(GearParamName("Fast fade time", "Время быстрого затухания"), "type_6_fast_fade_time")

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        try:
            self.maximum = await query_request(driver, QueryMinFastFadeTime(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read min fast fade time: {e}") from e
        return await super().get_schema(driver, address)


class Type6Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        return [
            DimmingCurveParam(),
            FastFadeTimeParam(),
        ]
