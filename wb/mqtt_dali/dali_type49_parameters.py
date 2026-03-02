# Type 49 Integrated power supply

from dali.address import GearShort

from .common_dali_device import MqttControl, MqttControlBase
from .dali_parameters import TypeParameters
from .device_publisher import ControlInfo
from .gear.integrated_power_supply import QueryActivePowerSupply
from .wbmqtt import ControlMeta


class Type49Parameters(TypeParameters):

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        return [
            MqttControl(
                control_info=ControlInfo(
                    "integrated_power_supply",
                    ControlMeta(
                        "switch",
                        "Integrated Power Supply",
                        read_only=True,
                    ),
                    "0",
                ),
                query_builder=lambda short_address: QueryActivePowerSupply(GearShort(short_address)),
                value_formatter=lambda response: "1" if response.value else "0",
            ),
        ]
