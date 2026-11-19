# Type 49 Integrated power supply

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
from .events import BusEvent, PowerSupplyRead
from .gear.integrated_power_supply import QueryActivePowerSupply
from .wbmqtt import ControlMeta, ControlState, TranslatedTitle


class IntegratedPowerSupplyControl(SingleQueryControl):
    """Whether the DT49 gear runs on its integrated power supply."""

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                "integrated_power_supply",
                ControlState(
                    ControlMeta(
                        "switch",
                        TranslatedTitle("Integrated Power Supply", "Встроенный источник питания"),
                        read_only=True,
                    ),
                    "0",
                ),
            ),
            query_builder=QueryActivePowerSupply,
            poll_interval=PERIODIC_STATUS_POLL_INTERVAL,
        )

    def notify(self, event: BusEvent) -> NotifyResult:
        return self._apply_quantity_read(
            event, PowerSupplyRead, lambda response: "1" if response.value else "0"
        )

    # --- Hooks for subclasses ---

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        return PowerSupplyRead(response, response is None)


class Type49Parameters(TypeParameters):

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [IntegratedPowerSupplyControl()]
