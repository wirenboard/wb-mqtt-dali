# Type 17 Dimming curve selection


from .extended_gear_parameters import DimmingCurveParam, TypeParameters
from .gear.dimming_curve import QueryDimmingCurve, SelectDimmingCurve


class Type17DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__("type_17_dimming_curve")


class Type17Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._parameters = [Type17DimmingCurveParam()]
