# Type 20 Demand response

from dali.address import GearShort

from .extended_gear_parameters import GearParam, TypeParameters
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


class LoadSheddingConditionParam(GearParam):
    name = "Load shedding condition"
    property_name = "type_20_load_shedding_condition"
    query_command_class = QueryLoadSheddingCondition
    set_command_class = SetLoadSheddingCondition

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
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
                    self.name: "Условие снижения нагрузки",
                    "no reduction": "не использовать коэффициент снижения",
                    "use reduction factor 1": "использовать коэффициент снижения 1",
                    "use reduction factor 2": "использовать коэффициент снижения 2",
                    "use reduction factor 3": "использовать коэффициент снижения 3",
                }
            },
        }


class ReductionFactor1Param(GearParam):
    name = "Reduction factor 1"
    property_name = "type_20_reduction_factor_1"
    query_command_class = QueryReductionFactor1
    set_command_class = SetReductionFactor1

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Коэффициент снижения 1",
                }
            },
        }


class ReductionFactor2Param(GearParam):
    name = "Reduction factor 2"
    property_name = "type_20_reduction_factor_2"
    query_command_class = QueryReductionFactor2
    set_command_class = SetReductionFactor2

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Коэффициент снижения 2",
                }
            },
        }


class ReductionFactor3Param(GearParam):
    name = "Reduction factor 3"
    property_name = "type_20_reduction_factor_3"
    query_command_class = QueryReductionFactor3
    set_command_class = SetReductionFactor3

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "title": self.name,
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                }
            },
            "translations": {
                "ru": {
                    self.name: "Коэффициент снижения 3",
                }
            },
        }


class Type20Parameters(TypeParameters):
    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list:
        return [
            LoadSheddingConditionParam(),
            ReductionFactor1Param(),
            ReductionFactor2Param(),
            ReductionFactor3Param(),
        ]
