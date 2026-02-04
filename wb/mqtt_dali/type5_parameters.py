# Type 5 Conversion from digital signal into d. c. voltage

from dali.address import GearShort
from dali.gear.converter import (
    QueryConverterFeatures,
    QueryDimmingCurve,
    SelectDimmingCurve,
)

from .extended_gear_parameters import NumberGearParam, TypeParameters
from .settings import SettingsParamName
from .wbdali import WBDALIDriver, query_request

# TODO: Output range is write only


class DimmingCurveParam(NumberGearParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Dimming curve", "Кривая диммирования"), "type_5_dimming_curve")

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


class Type5Parameters(TypeParameters):
    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        try:
            features = await query_request(driver, QueryConverterFeatures(address))
        except RuntimeError as e:
            raise RuntimeError(f"Failed to read converter features: {e}") from e
        if not ((features >> 5) & 1):  # 5th bit: dimming curve selectable
            return {}
        self._parameters = [DimmingCurveParam()]
        return await super().read(driver, address)
