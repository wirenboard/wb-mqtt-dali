# Type 20 Demand response

from .common_dali_device import MqttControl, MqttControlBase
from .dali_parameters import NumberGearParam, TypeParameters
from .device_publisher import ControlInfo
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
from .settings import SettingsParamName
from .utils import add_enum, add_translations
from .wbmqtt import ControlMeta, TranslatedTitle


class LoadSheddingConditionParam(NumberGearParam):
    query_command_class = QueryLoadSheddingCondition
    set_command_class = SetLoadSheddingCondition

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Load shedding condition", "Условие снижения нагрузки"),
            "type_20_load_shedding_condition",
        )

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(group_and_broadcast)
        add_enum(
            schema["properties"][self.property_name],
            [
                (0, "no reduction"),
                (1, "use reduction factor 1"),
                (2, "use reduction factor 2"),
                (3, "use reduction factor 3"),
            ],
        )
        add_translations(
            schema,
            "ru",
            {
                "no reduction": "не использовать коэффициент снижения",
                "use reduction factor 1": "использовать коэффициент снижения 1",
                "use reduction factor 2": "использовать коэффициент снижения 2",
                "use reduction factor 3": "использовать коэффициент снижения 3",
            },
        )
        return schema


class ReductionFactor1Param(NumberGearParam):
    query_command_class = QueryReductionFactor1
    set_command_class = SetReductionFactor1

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Reduction factor 1", "Коэффициент снижения 1"), "type_20_reduction_factor_1"
        )
        self.maximum = 100


class ReductionFactor2Param(NumberGearParam):
    query_command_class = QueryReductionFactor2
    set_command_class = SetReductionFactor2

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Reduction factor 2", "Коэффициент снижения 2"), "type_20_reduction_factor_2"
        )
        self.maximum = 100


class ReductionFactor3Param(NumberGearParam):
    query_command_class = QueryReductionFactor3
    set_command_class = SetReductionFactor3

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Reduction factor 3", "Коэффициент снижения 3"), "type_20_reduction_factor_3"
        )
        self.maximum = 100


class Type20Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()
        self._parameters = [
            LoadSheddingConditionParam(),
            ReductionFactor1Param(),
            ReductionFactor2Param(),
            ReductionFactor3Param(),
        ]

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [
            MqttControl(
                control_info=ControlInfo(
                    "load_shedding_condition",
                    ControlMeta(
                        title=TranslatedTitle("Load Shedding Condition", "Условие снижения нагрузки"),
                        read_only=True,
                        enum={
                            "0": TranslatedTitle("no reduction", "без снижения"),
                            "1": TranslatedTitle("reduction factor 1", "фактор снижения 1"),
                            "2": TranslatedTitle("reduction factor 2", "фактор снижения 2"),
                            "3": TranslatedTitle("reduction factor 3", "фактор снижения 3"),
                        },
                    ),
                    "0",
                ),
                query_builder=QueryLoadSheddingCondition,
                value_formatter=lambda response: str(response.value),
            ),
        ]
