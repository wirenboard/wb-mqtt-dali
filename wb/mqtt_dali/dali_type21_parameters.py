# Type 21 Thermal lamp protection

# pylint: disable=duplicate-code

from typing import Optional

from dali.command import Response

from .common_dali_device import (
    PERIODIC_STATUS_POLL_INTERVAL,
    MqttControlBase,
    NotifyResult,
    SingleQueryControl,
)
from .dali_parameters import TypeParameters
from .device_publisher import ControlInfo
from .events import BusEvent, ThermalLampProtectionRead
from .gear.thermal_lamp_protection import FailureStatusResponse, QueryFailureStatus
from .wbmqtt import ControlMeta, ControlState, TranslatedTitle


def _format_failure_status(value: Response) -> str:
    if isinstance(value, FailureStatusResponse):
        if getattr(value, "thermal_lamp_shutdown") is True:
            return "1"
        if getattr(value, "thermal_lamp_overload") is True:
            return "2"
    return "0"


class ThermalLampProtectionControl(SingleQueryControl):
    """The DT21 thermal protection state of the lamp, read on the periodic status interval."""

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                "thermal_lamp_protection",
                ControlState(
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
            ),
            query_builder=QueryFailureStatus,
            poll_interval=PERIODIC_STATUS_POLL_INTERVAL,
        )

    def notify(self, event: BusEvent) -> NotifyResult:
        return self._apply_quantity_read(event, ThermalLampProtectionRead, _format_failure_status)

    # --- Hooks for subclasses ---

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        return ThermalLampProtectionRead(response, response is None)


class Type21Parameters(TypeParameters):

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [ThermalLampProtectionControl()]
