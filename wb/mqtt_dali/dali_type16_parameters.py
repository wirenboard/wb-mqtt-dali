# Type 16 Thermal gear protection

from dali.address import GearShort
from dali.command import Response

from .common_dali_device import MqttControl
from .dali_parameters import TypeParameters
from .device_publisher import ControlInfo
from .gear.thermal_gear_protection import FailureStatusResponse, QueryFailureStatus
from .wbdali_utils import WBDALIDriver
from .wbmqtt import ControlMeta, TranslatedTitle


def _format_failure_status(value: Response) -> str:
    if isinstance(value, FailureStatusResponse):
        if getattr(value, "thermal gear shutdown") is True:
            return "1"
        if getattr(value, "thermal gear overload") is True:
            return "2"
    return "0"


class Type16Parameters(TypeParameters):

    async def get_mqtt_controls(self, driver: WBDALIDriver, short_address: int) -> list[MqttControl]:
        return [
            MqttControl(
                control_info=ControlInfo(
                    "thermal_gear_protection",
                    ControlMeta(
                        title="Thermal gear protection",
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
