# Type 8 RGBWAF

from dataclasses import dataclass
from typing import List, Optional

from dali import command
from dali.address import GearShort
from dali.gear.colour import Activate, SetTemporaryRGBDimLevel, SetTemporaryWAFDimLevel
from dali.gear.general import DTR0, DTR1, DTR2

from .common_dali_device import ControlPollResult, MqttControl, MqttControlBase
from .dali_type8_common import ColourComponent, Type8Limits
from .device_publisher import ControlInfo, ControlMeta
from .wbdali_utils import MASK

MAX_COLOUR_VALUE = MASK - 1


RGBW_COLOUR_COMPONENTS = [
    ColourComponent.RED,
    ColourComponent.GREEN,
    ColourComponent.BLUE,
    ColourComponent.WHITE,
]


def set_rgb_commands_builder(address: GearShort, red: int, green: int, blue: int) -> list[command.Command]:
    return [
        DTR0(red),
        DTR1(green),
        DTR2(blue),
        SetTemporaryRGBDimLevel(address),
    ]


def set_waf_commands_builder(
    address: GearShort, white: int, amber: int, free_colour: int
) -> list[command.Command]:
    return [
        DTR0(white),
        DTR1(amber),
        DTR2(free_colour),
        SetTemporaryWAFDimLevel(address),
    ]


@dataclass
class RgbwafColourValues:
    red: int = MASK
    green: int = MASK
    blue: int = MASK
    white: int = MASK
    amber: int = MASK
    free_colour: int = MASK
    components = RGBW_COLOUR_COMPONENTS

    def get_write_commands(self, address: GearShort) -> List[command.Command]:
        return set_rgb_commands_builder(address, self.red, self.green, self.blue) + set_waf_commands_builder(
            address, self.white, self.amber, self.free_colour
        )

    def to_json(self) -> dict:
        return {
            "rgb": f"{self.red};{self.green};{self.blue}",
            "white": self.white,
        }

    def from_json(self, value: dict) -> None:
        rgb_value = value.get("rgb")
        if rgb_value is not None:
            try:
                red_str, green_str, blue_str = rgb_value.split(";")
                self.red = int(red_str)
                self.green = int(green_str)
                self.blue = int(blue_str)
            except Exception as e:
                raise ValueError(f"Invalid RGB value: {rgb_value}") from e
        self.white = value.get("white", self.white)

    def get_schema(self, _limits: Type8Limits) -> dict:
        return {
            "properties": {
                "rgb": {
                    "type": "string",
                    "title": "RGB",
                    "format": "dali-rgb",
                    "propertyOrder": 2,
                    "options": {
                        "grid_columns": 2,
                    },
                },
                "white": {
                    "type": "integer",
                    "title": "White",
                    "format": "dali-white",
                    "minimum": 0,
                    "maximum": MASK,
                    "propertyOrder": 3,
                    "options": {
                        "grid_columns": 2,
                    },
                },
            },
            "required": ["rgb", "white"],
            "translations": {"ru": {"White": "Белый"}},
        }


def get_mqtt_controls() -> list[MqttControlBase]:

    def _set_rgb_commands_builder(short_address: int, value: str) -> list[command.Command]:
        components = value.split(";")
        if len(components) != 3:
            raise ValueError("RGB value must be in format 'R;G;B'")
        try:
            red, green, blue = (int(c) for c in components)
            red = min(red, MAX_COLOUR_VALUE)
            green = min(green, MAX_COLOUR_VALUE)
            blue = min(blue, MAX_COLOUR_VALUE)
        except ValueError as e:
            raise ValueError("RGB components must be integers") from e
        return set_rgb_commands_builder(GearShort(short_address), red, green, blue) + [
            Activate(GearShort(short_address)),
        ]

    def _set_white_commands_builder(short_address: int, value: str) -> list[command.Command]:
        try:
            white = int(value)
            white = min(white, MAX_COLOUR_VALUE)
        except ValueError as e:
            raise ValueError("white component must be integer") from e
        return set_waf_commands_builder(GearShort(short_address), white, MASK, MASK) + [
            Activate(GearShort(short_address)),
        ]

    return [
        MqttControl(
            ControlInfo(
                "current_rgb",
                ControlMeta("rgb", "Current RGB", read_only=True),
                "0;0;0",
            ),
        ),
        MqttControl(
            ControlInfo(
                "current_white",
                ControlMeta(title="Current White", read_only=True),
                "0",
            ),
        ),
        MqttControl(
            ControlInfo(
                "set_rgb",
                ControlMeta("rgb", "Wanted RGB"),
                "0;0;0",
            ),
            commands_builder=_set_rgb_commands_builder,
        ),
        MqttControl(
            ControlInfo(
                "set_white",
                ControlMeta("range", "Wanted White", minimum=0, maximum=MAX_COLOUR_VALUE),
                "0",
            ),
            commands_builder=_set_white_commands_builder,
        ),
    ]


def handle_poll_controls_result(new_colour: Optional[RgbwafColourValues]) -> list[ControlPollResult]:
    return [
        ControlPollResult(
            "current_rgb",
            (
                None
                if new_colour is None
                else ";".join([str(new_colour.red), str(new_colour.green), str(new_colour.blue)])
            ),
            error="r" if new_colour is None else None,
        ),
        ControlPollResult(
            "current_white",
            None if new_colour is None else str(new_colour.white),
            error="r" if new_colour is None else None,
        ),
    ]
