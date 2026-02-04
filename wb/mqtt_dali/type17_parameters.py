# Type 17 Dimming curve selection


from .extended_gear_parameters import NumberGearParam, TypeParameters
from .gear.dimming_curve import QueryDimmingCurve, SelectDimmingCurve
from .settings import SettingsParamName


class DimmingCurveParam(NumberGearParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Dimming curve", "Кривая диммирования"), "type_17_dimming_curve")

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


class Type17Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._parameters = [DimmingCurveParam()]
