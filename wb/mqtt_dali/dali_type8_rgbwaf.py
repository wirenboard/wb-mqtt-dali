# Type 8 RGBWAF

from dataclasses import dataclass
from typing import List

from dali import command
from dali.address import Address
from dali.gear.colour import Activate, SetTemporaryRGBDimLevel, SetTemporaryWAFDimLevel
from dali.gear.general import DTR0, DTR1, DTR2

from .common_dali_device import MqttControlBase
from .control_ids import CURRENT_RGB, CURRENT_WHITE, SET_RGB, SET_WHITE
from .dali_type8_common import ColourComponent
from .dali_type8_controls import ColourComponentControl, SingleComponentColourControl
from .device_publisher import ControlInfo
from .wbdali_utils import MASK
from .wbmqtt import ControlMeta, ControlState, TranslatedTitle

MAX_COLOUR_VALUE = MASK - 1

RGB_COMPONENTS = (ColourComponent.RED, ColourComponent.GREEN, ColourComponent.BLUE)

RGBW_COLOUR_COMPONENTS = [
    ColourComponent.RED,
    ColourComponent.GREEN,
    ColourComponent.BLUE,
    ColourComponent.WHITE,
]


def set_rgb_commands_builder(address: Address, red: int, green: int, blue: int) -> list[command.Command]:
    return [
        DTR0(red),
        DTR1(green),
        DTR2(blue),
        SetTemporaryRGBDimLevel(address),
    ]


def set_waf_commands_builder(
    address: Address, white: int, amber: int, free_colour: int
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

    def get_write_commands(self, address: Address) -> List[command.Command]:
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

    def get_schema(self, _limits) -> dict:
        return {
            "properties": {
                "rgb": {
                    "type": "string",
                    "title": "RGB",
                    "format": "dali-rgb",
                    "propertyOrder": 2,
                    "default": "255;255;255",
                    "options": {
                        "grid_columns": 2,
                    },
                },
                "white": {
                    "type": "integer",
                    "title": "W",
                    "format": "dali-white",
                    "minimum": 0,
                    "maximum": MASK,
                    "default": MASK,
                    "propertyOrder": 3,
                    "options": {
                        "grid_columns": 2,
                    },
                },
            },
            "required": ["rgb", "white"],
        }


def _set_rgb_commands_builder(short_address: Address, value: str) -> list[command.Command]:
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
    return set_rgb_commands_builder(short_address, red, green, blue) + [Activate(short_address)]


def _set_white_commands_builder(short_address: Address, value: str) -> list[command.Command]:
    try:
        white = int(value)
        white = min(white, MAX_COLOUR_VALUE)
    except ValueError as e:
        raise ValueError("W component must be integer") from e
    return set_waf_commands_builder(short_address, white, MASK, MASK) + [Activate(short_address)]


class _RgbControl(ColourComponentControl):
    """Shared RGB representation: the three components joined as ``r;g;b``."""

    # --- Hooks for subclasses ---

    def _format(self, components: dict[ColourComponent, int]) -> str:
        return ";".join(str(components[component]) for component in RGB_COMPONENTS)


class CurrentRgbControl(_RgbControl):
    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                CURRENT_RGB,
                ControlState(
                    ControlMeta("rgb", TranslatedTitle("Current RGB", "Текущий RGB"), read_only=True), "0;0;0"
                ),
            ),
            components=RGB_COMPONENTS,
            is_group_state_control=True,
        )


class SetRgbControl(_RgbControl):
    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                SET_RGB,
                ControlState(ControlMeta("rgb", TranslatedTitle("Wanted RGB", "Желаемый RGB")), "0;0;0"),
            ),
            components=RGB_COMPONENTS,
            commands_builder=_set_rgb_commands_builder,
        )


class CurrentWhiteControl(SingleComponentColourControl):
    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                CURRENT_WHITE,
                ControlState(
                    ControlMeta(title=TranslatedTitle("Current W", "Текущий W"), read_only=True), "0"
                ),
            ),
            component=ColourComponent.WHITE,
            is_group_state_control=True,
        )


class SetWhiteControl(SingleComponentColourControl):
    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                SET_WHITE,
                ControlState(
                    ControlMeta(
                        "range",
                        TranslatedTitle("Wanted W", "Желаемый W"),
                        minimum=0,
                        maximum=MAX_COLOUR_VALUE,
                    ),
                    "0",
                ),
            ),
            component=ColourComponent.WHITE,
            commands_builder=_set_white_commands_builder,
        )


def get_mqtt_controls(only_setup_controls: bool) -> list[MqttControlBase]:
    if only_setup_controls:
        return [SetRgbControl(), SetWhiteControl()]
    return [
        CurrentRgbControl(),
        SetRgbControl(),
        CurrentWhiteControl(),
        SetWhiteControl(),
    ]
