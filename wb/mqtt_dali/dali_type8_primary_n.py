# Type 8 Primary N

from dataclasses import dataclass
from typing import List

from dali import command
from dali.address import Address
from dali.gear.colour import Activate, SetTemporaryPrimaryNDimLevel
from dali.gear.general import DTR0, DTR1, DTR2

from .common_dali_device import MqttControlBase
from .control_ids import CURRENT_PRIMARY_N, PRIMARY_N_MAX, SET_PRIMARY_N
from .dali_type8_common import PRIMARY_N_BY_INDEX, ColourComponent
from .dali_type8_controls import SingleComponentColourControl
from .device_publisher import ControlInfo
from .wbdali_utils import MASK_2BYTES
from .wbmqtt import ControlMeta, ControlState, TranslatedTitle

COLOUR_NAMES = {
    ColourComponent.PRIMARY_N0: ("Primary N0", "Основной N0"),
    ColourComponent.PRIMARY_N1: ("Primary N1", "Основной N1"),
    ColourComponent.PRIMARY_N2: ("Primary N2", "Основной N2"),
    ColourComponent.PRIMARY_N3: ("Primary N3", "Основной N3"),
    ColourComponent.PRIMARY_N4: ("Primary N4", "Основной N4"),
    ColourComponent.PRIMARY_N5: ("Primary N5", "Основной N5"),
}


# Indexed by primary number, so the order is the one PRIMARY_N_BY_INDEX declares.
PRIMARY_N_COLOUR_COMPONENTS = list(PRIMARY_N_BY_INDEX.values())


def set_primary_n_commands_builder(address: Address, value: int, index: int) -> list[command.Command]:
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

    def get_write_commands(self, address: Address) -> List[command.Command]:
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

    def get_schema(self, _limits) -> dict:
        properties = {}
        required = []
        translations = {}
        for i, colour in enumerate(self.components):
            properties[colour.value] = {
                "type": "integer",
                "title": COLOUR_NAMES[colour][0],
                "minimum": 0,
                "maximum": MASK_2BYTES,
                "default": MASK_2BYTES,
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


def _set_primary_n_commands_builder(short_address: Address, value: str, index: int) -> list[command.Command]:
    try:
        primary_n = int(value)
    except ValueError as e:
        raise ValueError(f"primary N{index} must be integer") from e
    return set_primary_n_commands_builder(short_address, primary_n, index) + [Activate(short_address)]


class CurrentPrimaryNControl(SingleComponentColourControl):
    def __init__(self, index: int) -> None:
        super().__init__(
            ControlInfo(
                CURRENT_PRIMARY_N.format(index),
                ControlState(
                    ControlMeta(
                        title=TranslatedTitle(f"Current Primary N{index}", f"Текущий основной N{index}"),
                        read_only=True,
                    ),
                    "0",
                ),
            ),
            component=PRIMARY_N_COLOUR_COMPONENTS[index],
        )


class SetPrimaryNControl(SingleComponentColourControl):
    def __init__(self, index: int) -> None:
        super().__init__(
            ControlInfo(
                SET_PRIMARY_N.format(index),
                ControlState(
                    ControlMeta(
                        "range",
                        TranslatedTitle(f"Wanted Primary N{index}", f"Желаемый основной N{index}"),
                        minimum=0,
                        maximum=MASK_2BYTES,
                    ),
                    "0",
                ),
            ),
            component=PRIMARY_N_COLOUR_COMPONENTS[index],
            commands_builder=lambda address, value: _set_primary_n_commands_builder(address, value, index),
        )


def get_mqtt_controls() -> list[MqttControlBase]:
    res: list[MqttControlBase] = []
    for i in range(PRIMARY_N_MAX):
        res.append(CurrentPrimaryNControl(i))
        res.append(SetPrimaryNControl(i))
    return res
