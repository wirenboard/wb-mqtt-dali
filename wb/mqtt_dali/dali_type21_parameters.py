# Type 21 Thermal lamp protection

# pylint: disable=duplicate-code

from dali.command import Response

from .common_dali_device import MqttControl, MqttControlBase
from .dali_parameters import TypeParameters
from .device_publisher import ControlInfo
from .gear.thermal_lamp_protection import FailureStatusResponse, QueryFailureStatus
from .wbmqtt import ControlMeta, TranslatedTitle


def _format_failure_status(value: Response) -> str:
    if isinstance(value, FailureStatusResponse):
        if getattr(value, "thermal_lamp_shutdown") is True:
            return "1"
        if getattr(value, "thermal_lamp_overload") is True:
            return "2"
    return "0"


class Type21Parameters(TypeParameters):

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [
            MqttControl(
                control_info=ControlInfo(
                    "thermal_lamp_protection",
                    ControlMeta(
                        title=TranslatedTitle("Thermal lamp protection", "Тепловая защита лампы"),
                        read_only=True,
                        enum={
                            "0": TranslatedTitle("ok", "ок"),
                            "1": TranslatedTitle("shutdown", "отключение"),
                            "2": TranslatedTitle("overload", "перегрузка"),
                        },
                    ),
                    "0",
                ),
                query_builder=QueryFailureStatus,
                value_formatter=_format_failure_status,
            ),
        ]
