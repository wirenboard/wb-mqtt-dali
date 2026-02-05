# Type 4 Supply voltage controller for incandescent lamps

from dali.gear.incandescent import QueryDimmingCurve, SelectDimmingCurve

from .extended_gear_parameters import DimmingCurveParam, TypeParameters


class Type4DimmingCurveParam(DimmingCurveParam):
    query_command_class = QueryDimmingCurve
    set_command_class = SelectDimmingCurve

    def __init__(self) -> None:
        super().__init__("type_4_dimming_curve")


class Type4Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._parameters = [Type4DimmingCurveParam()]
