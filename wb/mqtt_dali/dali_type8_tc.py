# Type 8 Colour Temperature

import logging
from dataclasses import dataclass
from typing import List, Optional

from dali import command
from dali.address import Address
from dali.gear.colour import (
    Activate,
    ColourTemperatureTcStepCooler,
    ColourTemperatureTcStepWarmer,
    QueryColourValue,
    QueryColourValueDTR,
    SetTemporaryColourTemperature,
    tc_kelvin_mirek,
)
from dali.gear.general import DTR0, DTR1, QueryActualLevel, QueryContentDTR0

from .common_dali_device import ControlPollResult, MqttControl, MqttControlBase
from .dali_type8_common import MASK_2BYTES, ColourComponent, Type8Limits
from .device_publisher import ControlInfo, ControlMeta
from .wbdali_utils import WBDALIDriver, send_commands_with_retry
from .wbmqtt import TranslatedTitle

MAX_COLOUR_VALUE_2BYTES = MASK_2BYTES - 1


MAX_TC_MIREK = MAX_COLOUR_VALUE_2BYTES
MIN_TC_MIREK = 1


COLOR_TEMPERATURE_COLOUR_COMPONENTS = [
    ColourComponent.COLOUR_TEMPERATURE,
]


def set_colour_temperature_commands_builder(address: Address, value: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        SetTemporaryColourTemperature(address),
    ]


@dataclass
class ColourTemperatureValue:
    tc: int = MASK_2BYTES
    components = COLOR_TEMPERATURE_COLOUR_COMPONENTS

    def get_write_commands(self, address: Address) -> List[command.Command]:
        return set_colour_temperature_commands_builder(address, self.tc)

    def to_json(self) -> dict:
        return {
            "tc": self.tc,
        }

    def from_json(self, value: dict) -> None:
        self.tc = value.get("tc", self.tc)

    def get_schema(self, limits: Type8Limits) -> dict:
        return {
            "properties": {
                "tc": {
                    "type": "integer",
                    "title": "Colour temperature",
                    "default": MASK_2BYTES,
                    "format": "dali-tc",
                    "propertyOrder": 2,
                    "options": {
                        "grid_columns": 2,
                        "wb": {
                            "dali_tc": {
                                "minimum": limits.tc_min_mirek,
                                "maximum": limits.tc_max_mirek,
                            },
                        },
                    },
                },
            },
            "required": ["tc"],
            "translations": {"ru": {"Colour temperature": "Цветовая температура"}},
        }


def get_wanted_mqtt_controls(
    tc_min_mirek: int,
    tc_max_mirek: int,
) -> list[MqttControlBase]:
    def _set_colour_temperature_commands_builder(
        short_address: Address, value_k: str
    ) -> list[command.Command]:
        try:
            tc_k = max(int(value_k), 1)
            tc_mirek = tc_kelvin_mirek(tc_k)
            tc_mirek = min(tc_mirek, MAX_TC_MIREK)
            tc_mirek = max(tc_mirek, MIN_TC_MIREK)
        except ValueError as e:
            raise ValueError("colour temperature must be integer") from e
        return set_colour_temperature_commands_builder(short_address, tc_mirek) + [Activate(short_address)]

    return [
        MqttControl(
            ControlInfo(
                "set_colour_temperature",
                ControlMeta(
                    "range",
                    TranslatedTitle("Wanted Colour Temperature", "Желаемая цветовая температура"),
                    minimum=tc_kelvin_mirek(tc_max_mirek),
                    maximum=tc_kelvin_mirek(tc_min_mirek),
                    units="K",
                ),
                "4000",
            ),
            commands_builder=_set_colour_temperature_commands_builder,
        ),
    ]


def get_mqtt_controls(tc_min_mirek: int, tc_max_mirek: int) -> list[MqttControlBase]:

    return [
        MqttControl(
            ControlInfo(
                "current_colour_temperature",
                ControlMeta(
                    title=TranslatedTitle("Colour Temperature", "Цветовая температура"),
                    read_only=True,
                    units="K",
                ),
                "4000",
            ),
        ),
        *get_wanted_mqtt_controls(tc_min_mirek, tc_max_mirek),
        MqttControl(
            ControlInfo(
                "colour_temperature_step_warmer",
                ControlMeta("pushbutton", TranslatedTitle("Colour Temperature Step Warmer", "Теплее")),
                "0",
            ),
            commands_builder=lambda short_address, _: [ColourTemperatureTcStepWarmer(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "colour_temperature_step_cooler",
                ControlMeta("pushbutton", TranslatedTitle("Colour Temperature Step Cooler", "Холоднее")),
                "0",
            ),
            commands_builder=lambda short_address, _: [ColourTemperatureTcStepCooler(short_address)],
        ),
    ]


def handle_poll_controls_result(new_colour: Optional[ColourTemperatureValue]) -> list[ControlPollResult]:
    if new_colour is None or new_colour.tc == MASK_2BYTES:
        value = None
        error = "r"
    else:
        value = str(tc_kelvin_mirek(new_colour.tc))
        error = None
    return [
        ControlPollResult(
            "current_colour_temperature",
            value,
            error=error,
        ),
    ]


async def read_colour_temperature_limits_mirek(
    driver: WBDALIDriver,
    short_address: Address,
    logger: Optional[logging.Logger] = None,
) -> tuple[int, int]:
    cmds = [
        QueryActualLevel(short_address),
        DTR0(QueryColourValueDTR.ColourTemperatureTcWarmest),
        QueryColourValue(short_address),
        QueryContentDTR0(short_address),
        DTR0(QueryColourValueDTR.ColourTemperatureTcCoolest),
        QueryColourValue(short_address),
        QueryContentDTR0(short_address),
    ]
    resp = await send_commands_with_retry(driver, cmds, logger)
    warmest = MAX_TC_MIREK
    msb_item = resp[2]
    lsb_item = resp[3]
    if msb_item.raw_value is not None and lsb_item.raw_value is not None:
        warmest = (msb_item.raw_value.as_integer << 8) | lsb_item.raw_value.as_integer
    coolest = MIN_TC_MIREK
    msb_item = resp[5]
    lsb_item = resp[6]
    if msb_item.raw_value is not None and lsb_item.raw_value is not None:
        coolest = (msb_item.raw_value.as_integer << 8) | lsb_item.raw_value.as_integer
    return coolest, warmest
