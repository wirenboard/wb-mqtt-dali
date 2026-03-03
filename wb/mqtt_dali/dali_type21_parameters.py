# Type 21 Thermal lamp protection

from dali.address import GearShort
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
                        title="Thermal lamp protection",
                        read_only=True,
                        enum={
                            "0": TranslatedTitle("ok"),
                            "1": TranslatedTitle("shutdown"),
                            "2": TranslatedTitle("overload"),
                        },
                    ),
                    "0",
                ),
                query_builder=lambda short_address: QueryFailureStatus(GearShort(short_address)),
                value_formatter=_format_failure_status,
            ),
        ]
