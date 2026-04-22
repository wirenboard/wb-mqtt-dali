# Type 8 XY


from dataclasses import dataclass
from typing import List, Optional

from dali import command
from dali.address import Address
from dali.gear.colour import (
    Activate,
    SetTemporaryXCoordinate,
    SetTemporaryYCoordinate,
    XCoordinateStepDown,
    XCoordinateStepUp,
    YCoordinateStepDown,
    YCoordinateStepUp,
)
from dali.gear.general import DTR0, DTR1

from .common_dali_device import ControlPollResult, MqttControl, MqttControlBase
from .dali_type8_common import MASK_2BYTES, ColourComponent, Type8Limits
from .device_publisher import ControlInfo, ControlMeta
from .wbmqtt import TranslatedTitle

XY_COLOUR_COMPONENTS = [
    ColourComponent.X_COORDINATE,
    ColourComponent.Y_COORDINATE,
]


def set_x_coordinate_commands_builder(address: Address, value: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        SetTemporaryXCoordinate(address),
    ]


def set_y_coordinate_commands_builder(address: Address, value: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        SetTemporaryYCoordinate(address),
    ]


@dataclass
class XYColourValues:
    x_coordinate: int = MASK_2BYTES
    y_coordinate: int = MASK_2BYTES
    components = XY_COLOUR_COMPONENTS

    def get_write_commands(self, address: Address) -> List[command.Command]:
        res = set_x_coordinate_commands_builder(
            address, self.x_coordinate
        ) + set_y_coordinate_commands_builder(address, self.y_coordinate)
        return res

    def to_json(self) -> dict:
        return {
            "x_coordinate": self.x_coordinate,
            "y_coordinate": self.y_coordinate,
        }

    def from_json(self, value: dict) -> None:
        self.x_coordinate = value.get("x_coordinate", self.x_coordinate)
        self.y_coordinate = value.get("y_coordinate", self.y_coordinate)

    def get_schema(self, _limits: Type8Limits) -> dict:
        return {
            "properties": {
                "x_coordinate": {
                    "type": "integer",
                    "title": "X Coordinate",
                    "minimum": 0,
                    "maximum": MASK_2BYTES,
                    "default": MASK_2BYTES,
                    "propertyOrder": 2,
                    "options": {
                        "grid_columns": 2,
                    },
                },
                "y_coordinate": {
                    "type": "integer",
                    "title": "Y Coordinate",
                    "minimum": 0,
                    "maximum": MASK_2BYTES,
                    "default": MASK_2BYTES,
                    "propertyOrder": 3,
                    "options": {
                        "grid_columns": 2,
                    },
                },
            },
            "required": ["x_coordinate", "y_coordinate"],
            "translations": {"ru": {"X Coordinate": "Координата X", "Y Coordinate": "Координата Y"}},
        }


def get_mqtt_controls() -> list[MqttControlBase]:

    def _set_x_coordinate_commands_builder(short_address: Address, value: str) -> list[command.Command]:
        try:
            x_coordinate = int(value)
        except ValueError as e:
            raise ValueError("X coordinate must be integer") from e
        return set_x_coordinate_commands_builder(short_address, x_coordinate) + [
            Activate(short_address),
        ]

    def _set_y_coordinate_commands_builder(short_address: Address, value: str) -> list[command.Command]:
        try:
            y_coordinate = int(value)
        except ValueError as e:
            raise ValueError("Y coordinate must be integer") from e
        return set_y_coordinate_commands_builder(short_address, y_coordinate) + [
            Activate(short_address),
        ]

    return [
        MqttControl(
            ControlInfo(
                "current_x_coordinate",
                ControlMeta(
                    title=TranslatedTitle("Current X Coordinate", "Текущая координата X"),
                    read_only=True,
                ),
                "0",
            ),
        ),
        MqttControl(
            ControlInfo(
                "current_y_coordinate",
                ControlMeta(
                    title=TranslatedTitle("Current Y Coordinate", "Текущая координата Y"),
                    read_only=True,
                ),
                "0",
            ),
        ),
        MqttControl(
            ControlInfo(
                "x_coordinate_step_up",
                ControlMeta("pushbutton", TranslatedTitle("X Coordinate Step Up", "Координата X шаг вверх")),
                "0",
            ),
            commands_builder=lambda short_address, _: [XCoordinateStepUp(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "x_coordinate_step_down",
                ControlMeta("pushbutton", TranslatedTitle("X Coordinate Step Down", "Координата X шаг вниз")),
                "0",
            ),
            commands_builder=lambda short_address, _: [XCoordinateStepDown(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "y_coordinate_step_up",
                ControlMeta("pushbutton", TranslatedTitle("Y Coordinate Step Up", "Координата Y шаг вверх")),
                "0",
            ),
            commands_builder=lambda short_address, _: [YCoordinateStepUp(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "y_coordinate_step_down",
                ControlMeta("pushbutton", TranslatedTitle("Y Coordinate Step Down", "Координата Y шаг вниз")),
                "0",
            ),
            commands_builder=lambda short_address, _: [YCoordinateStepDown(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "set_x_coordinate",
                ControlMeta(
                    "range",
                    TranslatedTitle("Wanted X Coordinate", "Желаемая координата X"),
                    minimum=0,
                    maximum=MASK_2BYTES,
                ),
                "0",
            ),
            commands_builder=_set_x_coordinate_commands_builder,
        ),
        MqttControl(
            ControlInfo(
                "set_y_coordinate",
                ControlMeta(
                    "range",
                    TranslatedTitle("Wanted Y Coordinate", "Желаемая координата Y"),
                    minimum=0,
                    maximum=MASK_2BYTES,
                ),
                "0",
            ),
            commands_builder=_set_y_coordinate_commands_builder,
        ),
    ]


def handle_poll_controls_result(new_colour: Optional[XYColourValues]) -> list[ControlPollResult]:
    return [
        ControlPollResult(
            "current_x_coordinate",
            None if new_colour is None else str(new_colour.x_coordinate),
            error="r" if new_colour is None else None,
        ),
        ControlPollResult(
            "current_y_coordinate",
            None if new_colour is None else str(new_colour.y_coordinate),
            error="r" if new_colour is None else None,
        ),
    ]
