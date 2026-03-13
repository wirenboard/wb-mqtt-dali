# Type 8 Primary N

from dataclasses import dataclass
from typing import List, Optional

from dali import command
from dali.address import GearShort
from dali.gear.colour import Activate, SetTemporaryPrimaryNDimLevel
from dali.gear.general import DTR0, DTR1, DTR2

from .common_dali_device import ControlPollResult, MqttControl, MqttControlBase
from .dali_type8_common import MASK_2BYTES, ColourComponent, Type8Limits
from .device_publisher import ControlInfo, ControlMeta

COLOUR_NAMES = {
    ColourComponent.PRIMARY_N0: ("Primary N0", "Основной N0"),
    ColourComponent.PRIMARY_N1: ("Primary N1", "Основной N1"),
    ColourComponent.PRIMARY_N2: ("Primary N2", "Основной N2"),
    ColourComponent.PRIMARY_N3: ("Primary N3", "Основной N3"),
    ColourComponent.PRIMARY_N4: ("Primary N4", "Основной N4"),
    ColourComponent.PRIMARY_N5: ("Primary N5", "Основной N5"),
}


PRIMARY_N_COLOUR_COMPONENTS = [
    ColourComponent.PRIMARY_N0,
    ColourComponent.PRIMARY_N1,
    ColourComponent.PRIMARY_N2,
    ColourComponent.PRIMARY_N3,
    ColourComponent.PRIMARY_N4,
    ColourComponent.PRIMARY_N5,
]


def set_primary_n_commands_builder(address: GearShort, value: int, index: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        DTR2(index),
        SetTemporaryPrimaryNDimLevel(address),
    ]


@dataclass
class PrimaryNColourValues:
    primary_n0: int = MASK_2BYTES
    primary_n1: int = MASK_2BYTES
    primary_n2: int = MASK_2BYTES
    primary_n3: int = MASK_2BYTES
    primary_n4: int = MASK_2BYTES
    primary_n5: int = MASK_2BYTES
    components = PRIMARY_N_COLOUR_COMPONENTS

    def get_write_commands(self, address: GearShort) -> List[command.Command]:
        res = []
        for colour in self.components:
            value = getattr(self, colour.value)
            index = int(colour.value[-1])  # primary_n0 -> 0
            res.extend(set_primary_n_commands_builder(address, value, index))
        return res

    def to_json(self) -> dict:
        return {colour.value: getattr(self, colour.value) for colour in self.components}

    def from_json(self, value: dict) -> None:
        for colour in self.components:
            if colour.value in value:
                setattr(self, colour.value, value[colour.value])

    def get_schema(self, _limits: Type8Limits) -> dict:
        properties = {}
        required = []
        translations = {}
        for i, colour in enumerate(self.components):
            properties[colour.value] = {
                "type": "integer",
                "title": COLOUR_NAMES[colour][0],
                "minimum": 0,
                "maximum": MASK_2BYTES,
                "propertyOrder": i + 2,
                "options": {
                    "grid_columns": 2,
                },
            }
            required.append(colour.value)
            translations[COLOUR_NAMES[colour][0]] = COLOUR_NAMES[colour][1]
        return {
            "properties": properties,
            "required": required,
            "translations": {"ru": translations},
        }


def get_mqtt_controls() -> list[MqttControlBase]:

    def _set_primary_n_commands_builder(short_address: int, value: str, index: int) -> list[command.Command]:
        try:
            primary_n = int(value)
        except ValueError as e:
            raise ValueError(f"primary N{index} must be integer") from e
        return set_primary_n_commands_builder(GearShort(short_address), primary_n, index) + [
            Activate(GearShort(short_address)),
        ]

    res = []
    for i in range(6):
        res.append(
            MqttControl(
                ControlInfo(
                    f"current_primary_n{i}",
                    ControlMeta(title=f"Current Primary N{i}", read_only=True),
                    "0",
                ),
            ),
        )
        res.append(
            MqttControl(
                ControlInfo(
                    f"set_primary_n{i}",
                    ControlMeta("range", f"Wanted Primary N{i}", minimum=0, maximum=MASK_2BYTES),
                    "0",
                ),
                commands_builder=lambda short_address, value, index=i: _set_primary_n_commands_builder(
                    short_address, value, index
                ),
            )
        )
    return res


def handle_poll_controls_result(new_colour: Optional[PrimaryNColourValues]) -> list[ControlPollResult]:
    return [
        ControlPollResult(
            f"current_primary_n{i}",
            None if new_colour is None else str(getattr(new_colour, f"primary_n{i}")),
            error="r" if new_colour is None else None,
        )
        for i, _ in enumerate(PRIMARY_N_COLOUR_COMPONENTS)
    ]
