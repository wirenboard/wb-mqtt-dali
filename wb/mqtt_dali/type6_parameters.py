# Type 6 LED modules

from dali.gear.led import (
    QueryDimmingCurve,
    QueryFastFadeTime,
    QueryMinFastFadeTime,
    SelectDimmingCurve,
    StoreDTRAsFastFadeTime,
)

from .extended_gear_parameters import NumberGearParam, TypeParameters
from .settings import SettingsParamAddress, SettingsParamName
from .wbdali import WBDALIDriver, query_request


class DimmingCurveParam(NumberGearParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Dimming curve", "Кривая диммирования"), "type_6_dimming_curve")

    def get_schema(self) -> dict:
        schema = super().get_schema()
        schema["properties"][self.property_name]["enum"] = [0, 1]
        if "options" not in schema["properties"][self.property_name]:
            schema["properties"][self.property_name]["options"] = {}
        schema["properties"][self.property_name]["options"] = {
            "enum_titles": ["standard", "linear"],
        }
        schema["translations"]["ru"]["standard"] = "стандартная"
        schema["translations"]["ru"]["linear"] = "линейная"
        return schema


class FastFadeTimeParam(NumberGearParam):
    query_command_class = QueryFastFadeTime
    set_command_class = StoreDTRAsFastFadeTime

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Fast fade time", "Время быстрого затухания"), "type_6_fast_fade_time"
        )

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        res = await super().read(driver, address)
        try:
            self.maximum = await query_request(driver, QueryMinFastFadeTime(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read min fast fade time: {e}") from e
        return res


class Type6Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._parameters = [
            DimmingCurveParam(),
            FastFadeTimeParam(),
        ]
