# Type 16 Thermal gear protection

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
from .events import BusEvent, ThermalGearProtectionRead
from .gear.thermal_gear_protection import FailureStatusResponse, QueryFailureStatus
from .wbmqtt import ControlMeta, ControlState, TranslatedTitle


def _format_failure_status(value: Response) -> str:
    if isinstance(value, FailureStatusResponse):
        if getattr(value, "thermal_gear_shutdown") is True:
            return "1"
        if getattr(value, "thermal_gear_overload") is True:
            return "2"
    return "0"


class ThermalGearProtectionControl(SingleQueryControl):
    """The DT16 thermal protection state of the gear, read on the periodic status interval."""

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                "thermal_gear_protection",
                ControlState(
                    ControlMeta(
                        title=TranslatedTitle("Thermal gear protection", "Тепловая защита"),
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
        return self._apply_quantity_read(event, ThermalGearProtectionRead, _format_failure_status)

    # --- Hooks for subclasses ---

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        return ThermalGearProtectionRead(response, response is None)


class Type16Parameters(TypeParameters):

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [ThermalGearProtectionControl()]
