# Type 20 Demand response

from dali.address import GearShort

from .extended_gear_parameters import (
    GearParamBase,
    GearParamName,
    NumberGearParam,
    TypeParameters,
)
from .gear.demand_response import (
    QueryLoadSheddingCondition,
    QueryReductionFactor1,
    QueryReductionFactor2,
    QueryReductionFactor3,
    SetLoadSheddingCondition,
    SetReductionFactor1,
    SetReductionFactor2,
    SetReductionFactor3,
)
from .wbdali import WBDALIDriver


class LoadSheddingConditionParam(NumberGearParam):
    query_command_class = QueryLoadSheddingCondition
    set_command_class = SetLoadSheddingCondition

    def __init__(self) -> None:
        super().__init__(GearParamName("Load shedding condition"), "type_20_load_shedding_condition")

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name.en,
                    "type": "integer",
                    "enum": [0, 1, 2, 3],
                    "options": {
                        "enum_titles": [
                            "no reduction",
                            "use reduction factor 1",
                            "use reduction factor 2",
                            "use reduction factor 3",
                        ]
                    },
                }
            },
            "translations": {
                "ru": {
                    self.name.en: "Условие снижения нагрузки",
                    "no reduction": "не использовать коэффициент снижения",
                    "use reduction factor 1": "использовать коэффициент снижения 1",
                    "use reduction factor 2": "использовать коэффициент снижения 2",
                    "use reduction factor 3": "использовать коэффициент снижения 3",
                }
            },
        }


class ReductionFactor1Param(NumberGearParam):
    query_command_class = QueryReductionFactor1
    set_command_class = SetReductionFactor1

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Reduction factor 1", "Коэффициент снижения 1"), "type_20_reduction_factor_1"
        )
        self.maximum = 100


class ReductionFactor2Param(NumberGearParam):
    query_command_class = QueryReductionFactor2
    set_command_class = SetReductionFactor2

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Reduction factor 2", "Коэффициент снижения 2"), "type_20_reduction_factor_2"
        )
        self.maximum = 100


class ReductionFactor3Param(NumberGearParam):
    query_command_class = QueryReductionFactor3
    set_command_class = SetReductionFactor3

    def __init__(self) -> None:
        super().__init__(
            GearParamName("Reduction factor 3", "Коэффициент снижения 3"), "type_20_reduction_factor_3"
        )
        self.maximum = 100


class Type20Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        return [
            LoadSheddingConditionParam(),
            ReductionFactor1Param(),
            ReductionFactor2Param(),
            ReductionFactor3Param(),
        ]
