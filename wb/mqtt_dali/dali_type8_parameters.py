# Type 8 Colour control

import asyncio
import enum
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Callable, Generator, List, Optional, Union

from dali import command
from dali.address import GearShort
from dali.gear.colour import (
    Activate,
    ColourTemperatureTcStepCooler,
    ColourTemperatureTcStepWarmer,
    QueryColourStatus,
    QueryColourValue,
    QueryColourValueDTR,
    SetTemporaryColourTemperature,
    SetTemporaryPrimaryNDimLevel,
    SetTemporaryRGBDimLevel,
    SetTemporaryWAFDimLevel,
    SetTemporaryXCoordinate,
    SetTemporaryYCoordinate,
    XCoordinateStepDown,
    XCoordinateStepUp,
    YCoordinateStepDown,
    YCoordinateStepUp,
)
from dali.gear.general import (
    DTR0,
    DTR1,
    DTR2,
    QueryActualLevel,
    QueryContentDTR0,
    QueryPowerOnLevel,
    QuerySceneLevel,
    QuerySystemFailureLevel,
    SetPowerOnLevel,
    SetScene,
    SetSystemFailureLevel,
)

from .common_dali_device import ControlPollResult, MqttControl, MqttControlBase
from .dali_common_parameters import SCENES_TOTAL
from .dali_parameters import TypeParameters
from .device_publisher import ControlInfo, ControlMeta
from .settings import SettingsParamBase, SettingsParamName
from .wbdali_utils import MASK, WBDALIDriver, query_response


class ColourType(enum.Enum):
    XY = 0x10
    COLOUR_TEMPERATURE = 0x20
    PRIMARY_N = 0x40
    RGBWAF = 0x80


class ColourComponent(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    WHITE = "white"
    AMBER = "amber"
    FREE_COLOUR = "free_colour"
    COLOUR_TEMPERATURE = "tc"
    PRIMARY_N0 = "primary_n0"
    PRIMARY_N1 = "primary_n1"
    PRIMARY_N2 = "primary_n2"
    PRIMARY_N3 = "primary_n3"
    PRIMARY_N4 = "primary_n4"
    PRIMARY_N5 = "primary_n5"
    X_COORDINATE = "x_coordinate"
    Y_COORDINATE = "y_coordinate"


COLOUR_NAMES = {
    ColourComponent.RED: ("Red", "Красный"),
    ColourComponent.GREEN: ("Green", "Зеленый"),
    ColourComponent.BLUE: ("Blue", "Синий"),
    ColourComponent.WHITE: ("White", "Белый"),
    ColourComponent.AMBER: ("Amber", "Янтарный"),
    ColourComponent.FREE_COLOUR: ("Free colour", "Свободный цвет"),
    ColourComponent.COLOUR_TEMPERATURE: ("Colour temperature", "Цветовая температура"),
    ColourComponent.PRIMARY_N0: ("Primary N0", "Основной N0"),
    ColourComponent.PRIMARY_N1: ("Primary N1", "Основной N1"),
    ColourComponent.PRIMARY_N2: ("Primary N2", "Основной N2"),
    ColourComponent.PRIMARY_N3: ("Primary N3", "Основной N3"),
    ColourComponent.PRIMARY_N4: ("Primary N4", "Основной N4"),
    ColourComponent.PRIMARY_N5: ("Primary N5", "Основной N5"),
    ColourComponent.X_COORDINATE: ("X Coordinate", "Координата X"),
    ColourComponent.Y_COORDINATE: ("Y Coordinate", "Координата Y"),
}

RGBW_COLOUR_COMPONENTS = [
    ColourComponent.RED,
    ColourComponent.GREEN,
    ColourComponent.BLUE,
    ColourComponent.WHITE,
]

COLOR_TEMPERATURE_COLOUR_COMPONENTS = [
    ColourComponent.COLOUR_TEMPERATURE,
]

PRIMARY_N_COLOUR_COMPONENTS = [
    ColourComponent.PRIMARY_N0,
    ColourComponent.PRIMARY_N1,
    ColourComponent.PRIMARY_N2,
    ColourComponent.PRIMARY_N3,
    ColourComponent.PRIMARY_N4,
    ColourComponent.PRIMARY_N5,
]

XY_COLOUR_COMPONENTS = [
    ColourComponent.X_COORDINATE,
    ColourComponent.Y_COORDINATE,
]

ACTUAL_LEVEL_COLOUR_TAGS = {
    ColourComponent.RED: QueryColourValueDTR.RedDimLevel,
    ColourComponent.GREEN: QueryColourValueDTR.GreenDimLevel,
    ColourComponent.BLUE: QueryColourValueDTR.BlueDimLevel,
    ColourComponent.WHITE: QueryColourValueDTR.WhiteDimLevel,
    ColourComponent.AMBER: QueryColourValueDTR.AmberDimLevel,
    ColourComponent.FREE_COLOUR: QueryColourValueDTR.FreecolourDimLevel,
    ColourComponent.COLOUR_TEMPERATURE: QueryColourValueDTR.ColourTemperatureTC,
    ColourComponent.PRIMARY_N0: QueryColourValueDTR.PrimaryNDimLevel0,
    ColourComponent.PRIMARY_N1: QueryColourValueDTR.PrimaryNDimLevel1,
    ColourComponent.PRIMARY_N2: QueryColourValueDTR.PrimaryNDimLevel2,
    ColourComponent.PRIMARY_N3: QueryColourValueDTR.PrimaryNDimLevel3,
    ColourComponent.PRIMARY_N4: QueryColourValueDTR.PrimaryNDimLevel4,
    ColourComponent.PRIMARY_N5: QueryColourValueDTR.PrimaryNDimLevel5,
    ColourComponent.X_COORDINATE: QueryColourValueDTR.XCoordinate,
    ColourComponent.Y_COORDINATE: QueryColourValueDTR.YCoordinate,
}

REPORT_COLOUR_TAGS = {
    ColourComponent.RED: QueryColourValueDTR.ReportRedDimLevel,
    ColourComponent.GREEN: QueryColourValueDTR.ReportGreenDimLevel,
    ColourComponent.BLUE: QueryColourValueDTR.ReportBlueDimLevel,
    ColourComponent.WHITE: QueryColourValueDTR.ReportWhiteDimLevel,
    ColourComponent.AMBER: QueryColourValueDTR.ReportAmberDimLevel,
    ColourComponent.FREE_COLOUR: QueryColourValueDTR.ReportFreecolourDimLevel,
    ColourComponent.COLOUR_TEMPERATURE: QueryColourValueDTR.ReportColourTemperatureTc,
    ColourComponent.PRIMARY_N0: QueryColourValueDTR.PrimaryNDimLevel0,
    ColourComponent.PRIMARY_N1: QueryColourValueDTR.PrimaryNDimLevel1,
    ColourComponent.PRIMARY_N2: QueryColourValueDTR.PrimaryNDimLevel2,
    ColourComponent.PRIMARY_N3: QueryColourValueDTR.PrimaryNDimLevel3,
    ColourComponent.PRIMARY_N4: QueryColourValueDTR.PrimaryNDimLevel4,
    ColourComponent.PRIMARY_N5: QueryColourValueDTR.PrimaryNDimLevel5,
    ColourComponent.X_COORDINATE: QueryColourValueDTR.ReportXCoordinate,
    ColourComponent.Y_COORDINATE: QueryColourValueDTR.ReportYCoordinate,
}


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


def set_colour_temperature_commands_builder(address: GearShort, value: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        SetTemporaryColourTemperature(address),
    ]


@dataclass
class ColourTemperatureValue:
    tc: int = MASK
    components = COLOR_TEMPERATURE_COLOUR_COMPONENTS

    def get_write_commands(self, address: GearShort) -> List[command.Command]:
        return set_colour_temperature_commands_builder(address, self.tc)


def set_primary_n_commands_builder(address: GearShort, value: int, index: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        DTR2(index),
        SetTemporaryPrimaryNDimLevel(address),
    ]


@dataclass
class PrimaryNColourValues:
    primary_n0: int = MASK
    primary_n1: int = MASK
    primary_n2: int = MASK
    primary_n3: int = MASK
    primary_n4: int = MASK
    primary_n5: int = MASK
    components = PRIMARY_N_COLOUR_COMPONENTS

    def get_write_commands(self, address: GearShort) -> List[command.Command]:
        res = []
        for colour in self.components:
            value = getattr(self, colour.value)
            index = int(colour.value[-1])  # primary_n0 -> 0
            res.extend(set_primary_n_commands_builder(address, value, index))
        return res


def set_x_coordinate_commands_builder(address: GearShort, value: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        SetTemporaryXCoordinate(address),
    ]


def set_y_coordinate_commands_builder(address: GearShort, value: int) -> list[command.Command]:
    return [
        DTR0((value & 0xFF)),
        DTR1((value >> 8) & 0xFF),
        SetTemporaryYCoordinate(address),
    ]


@dataclass
class XYColourValues:
    x_coordinate: int = MASK
    y_coordinate: int = MASK
    components = XY_COLOUR_COMPONENTS

    def get_write_commands(self, address: GearShort) -> List[command.Command]:
        res = set_x_coordinate_commands_builder(
            address, self.x_coordinate
        ) + set_y_coordinate_commands_builder(address, self.y_coordinate)
        return res


@dataclass
class ColourSettings:
    colour_type: ColourType
    colour: Union[RgbwafColourValues, ColourTemperatureValue, PrimaryNColourValues, XYColourValues]
    level: int

    def __init__(self, colour_type: ColourType, level: int = MASK) -> None:
        self.colour_type = colour_type
        self.level = level
        if colour_type == ColourType.RGBWAF:
            self.colour = RgbwafColourValues()
        elif colour_type == ColourType.COLOUR_TEMPERATURE:
            self.colour = ColourTemperatureValue()
        elif colour_type == ColourType.PRIMARY_N:
            self.colour = PrimaryNColourValues()
        elif colour_type == ColourType.XY:
            self.colour = XYColourValues()
        else:
            raise RuntimeError(f"Unsupported colour type: {colour_type}")

    def to_json(self) -> dict:
        if self.colour_type == ColourType.RGBWAF:
            res = {
                "rgb": f"{self.colour.red};{self.colour.green};{self.colour.blue}",
                "white": self.colour.white,
            }
        else:
            res = asdict(self.colour)
        res["level"] = self.level
        return res


def get_max_colour_value(colour_type: ColourType) -> int:
    if colour_type == ColourType.RGBWAF:
        return 255
    return 65535


def query_colour_with_level(
    address: GearShort,
    cmd: command.Command,
    colour_tags: dict[ColourComponent, QueryColourValueDTR],
    default_colour_type: ColourType,
) -> Generator[
    Union[command.Command, List[command.Command]],
    Union[Optional[command.Response], List[Optional[command.Response]]],
    ColourSettings,
]:
    resp = yield [cmd, DTR0(QueryColourValueDTR.ReportColourType), QueryColourValue(address)]

    if resp[0] is None or resp[0].raw_value is None:
        raise RuntimeError(f"Failed to get {cmd}")
    level = resp[0].raw_value.as_integer

    if resp[-1] is None or resp[-1].raw_value is None:
        raise RuntimeError(f"Failed to get colour type for {cmd}")

    colour_type = resp[-1].raw_value.as_integer
    # Colour type is set to MASK, so all colours are also MASK
    if MASK == colour_type:
        return ColourSettings(default_colour_type, level)

    res = ColourSettings(ColourType(colour_type), level)

    cmds = []
    for colour_component in res.colour.components:
        cmds.append(DTR0(colour_tags[colour_component]))
        cmds.append(QueryColourValue(address))
        # Only RGBWAF has one byte colour components
        if res.colour_type != ColourType.RGBWAF:
            cmds.append(QueryContentDTR0(address))

    resp = yield cmds

    try:
        resp_iter = iter(resp)
        for colour_component in res.colour.components:
            # Pass DTR0 response
            next(resp_iter)
            # QueryColourValue response
            msb_item = next(resp_iter)
            if msb_item is None or msb_item.raw_value is None:
                raise RuntimeError(f"Failed to get {colour_component.value} for {cmd}")
            value = msb_item.raw_value.as_integer
            if res.colour_type != ColourType.RGBWAF:
                value <<= 8
                # QueryContentDTR0 response
                lsb_item = next(resp_iter)
                if lsb_item is None or lsb_item.raw_value is None:
                    raise RuntimeError(f"Failed to get {colour_component.value} LSB for {cmd}")
                value |= lsb_item.raw_value.as_integer
            setattr(res.colour, colour_component.value, value)
    except StopIteration as e:
        raise RuntimeError("Unexpected end of responses") from e
    return res


class ColourState(SettingsParamBase):
    def __init__(
        self,
        name: SettingsParamName,
        property_name: str,
        query_command_class: Callable[[GearShort], command.Command],
        setup_command_class: Callable[[GearShort], command.Command],
        colour_tags: dict[ColourComponent, QueryColourValueDTR],
        property_order: int,
        default_colour_type: ColourType,
        read_after_save: bool = True,
    ) -> None:
        super().__init__(name)
        self.property_name = property_name
        self.value: Optional[ColourSettings] = None
        self._query_command_class = query_command_class
        self._setup_command_class = setup_command_class
        self._property_order = property_order
        self._colour_tags = colour_tags
        self._read_after_save = read_after_save
        self._default_colour_type = default_colour_type

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        return await self._read_impl(driver, short_address)

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        if self.property_name not in value:
            return {}
        if self.value is None or self.value.colour is None:
            raise RuntimeError(f"Cannot write {self.name.en} before reading it")
        values = value.get(self.property_name, {})
        new_state = deepcopy(self.value)
        if new_state.colour_type == ColourType.RGBWAF:
            rgb_value = values.get("rgb")
            if rgb_value is not None:
                try:
                    red_str, green_str, blue_str = rgb_value.split(";")
                    new_state.colour.red = int(red_str)
                    new_state.colour.green = int(green_str)
                    new_state.colour.blue = int(blue_str)
                except Exception as e:
                    raise ValueError(f"Invalid RGB value: {rgb_value}") from e
            new_state.colour.white = values.get("white", new_state.colour.white)
        else:
            for colour in new_state.colour.components:
                if colour.value in values:
                    setattr(new_state.colour, colour.value, values[colour.value])
        new_state.level = values.get("level", self.value.level)
        if new_state == self.value:
            return {}
        address = GearShort(short_address)
        cmds = new_state.colour.get_write_commands(address)
        cmds.extend([DTR0(new_state.level), self._setup_command_class(address)])
        await driver.send_commands(cmds)
        if self._read_after_save:
            return await self._read_impl(driver, short_address)
        self.value = new_state
        return {self.property_name: new_state.to_json()}

    def get_schema(self) -> dict:
        schema = {
            "properties": {
                self.property_name: {
                    "type": "object",
                    "title": self.name.en,
                    "properties": {
                        "level": {
                            "type": "integer",
                            "title": "Light level",
                            "format": "dali-level",
                            "propertyOrder": 0,
                            "minimum": 0,
                            "maximum": 255,
                            "options": {
                                "grid_columns": 1,
                            },
                        }
                    },
                    "propertyOrder": self._property_order,
                    "required": [],
                },
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "Light level": "Уровень яркости",
                },
            },
        }
        if self.value is not None and self.value.colour is not None:
            root_property = schema["properties"][self.property_name]
            if self.value.colour_type == ColourType.RGBWAF:
                root_property["properties"]["rgb"] = {
                    "type": "string",
                    "title": "RGB",
                    "format": "rgb",
                    "propertyOrder": 1,
                    "options": {
                        "grid_columns": 2,
                    },
                }
                root_property["properties"]["white"] = {
                    "type": "integer",
                    "title": "White",
                    "minimum": 0,
                    "maximum": 255,
                    "propertyOrder": 2,
                    "options": {
                        "grid_columns": 2,
                    },
                }
                root_property["required"].append("rgb")
                root_property["required"].append("white")
            else:
                for index, colour in enumerate(self.value.colour.components):
                    root_property["properties"][colour.value] = {
                        "type": "integer",
                        "title": COLOUR_NAMES[colour][0],
                        "minimum": 0,
                        "maximum": 65535,
                        "propertyOrder": index + 1,
                        "options": {
                            "grid_columns": 2,
                        },
                    }
                    root_property["required"].append(colour.value)
        return schema

    async def _read_impl(self, driver: WBDALIDriver, short_address: int) -> dict:
        address = GearShort(short_address)
        resp = await driver.run_sequence(
            query_colour_with_level(
                address, self._query_command_class(address), self._colour_tags, self._default_colour_type
            )
        )
        if resp is None:
            raise RuntimeError(f"Error reading {self.name.en}")
        self.value = resp
        return {self.property_name: resp.to_json()}

class SceneSettings(ColourState):
    def __init__(self, scene_number: int, default_colour_type: ColourType) -> None:
        super().__init__(
            SettingsParamName(f"Scene {scene_number}"),
            f"scene_{scene_number}",
            lambda address: QuerySceneLevel(address, scene_number),
            lambda address: SetScene(address, scene_number),
            REPORT_COLOUR_TAGS,
            900,
            default_colour_type,
        )
        self._scene_number = scene_number

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        return self._to_json(await super().read(driver, short_address))

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        values_to_set = deepcopy(value)
        if value.get("enabled", True):
            values_to_set["level"] = value.get("level", 0)
        else:
            values_to_set["level"] = MASK
        res = await super().write(driver, short_address, {self.property_name: values_to_set})
        return self._to_json(res)

    def _to_json(self, read_response: dict) -> dict:
        value = read_response.get(self.property_name, {})
        if not value:
            return value
        level = value.get("level", MASK)
        value["enabled"] = level != MASK
        if level == MASK:
            value["level"] = 0
        return value


class ScenesSettings(SettingsParamBase):
    def __init__(self, default_colour_type: ColourType) -> None:
        super().__init__(SettingsParamName("Scenes", "Сцены"))

        self.property_name = "scenes"
        self._scenes = [SceneSettings(i, default_colour_type) for i in range(SCENES_TOTAL)]
        self._scene_values = [{} for _ in range(SCENES_TOTAL)]

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        self._scene_values = await asyncio.gather(
            *[scene.read(driver, short_address) for scene in self._scenes]
        )
        return {self.property_name: self._scene_values}

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        if self._scenes[0].value is None:
            raise RuntimeError("Cannot write scenes before reading them")
        if self.property_name not in value:
            return {}
        results = await asyncio.gather(
            *[
                scene.write(driver, short_address, scene_value)
                for scene, scene_value in zip(self._scenes, value.get(self.property_name, []))
            ]
        )
        for i, res in enumerate(results):
            self._scene_values[i].update(res)
        return {self.property_name: self._scene_values}

    def get_schema(self) -> dict:
        if self._scenes[0].value is None:
            raise RuntimeError("Cannot get schema for scenes before reading them")
        schema = {
            "properties": {
                self.property_name: {
                    "type": "array",
                    "title": self.name.en,
                    "format": "table",
                    "minItems": SCENES_TOTAL,
                    "maxItems": SCENES_TOTAL,
                    "items": {
                        "type": "object",
                        "properties": {
                            "enabled": {
                                "type": "boolean",
                                "title": "Part of the scene",
                                "propertyOrder": 1,
                                "format": "switch",
                                "options": {
                                    "compact": True,
                                    "grid_columns": 2,
                                },
                            },
                            "level": {
                                "type": "integer",
                                "title": "Light level",
                                "format": "dali-level",
                                "propertyOrder": 2,
                                "minimum": 0,
                                "maximum": 254,
                            },
                        },
                        "required": ["enabled", "level"],
                    },
                    "propertyOrder": 900,
                },
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "Part of the scene": "Часть сцены",
                    "Light level": "Яркость",
                }
            },
        }
        order = 3
        max_value = get_max_colour_value(self._scenes[0].value.colour_type)
        for colour in self._scenes[0].value.colour.components:
            schema["properties"][self.property_name]["items"]["properties"][colour.value] = {
                "type": "integer",
                "title": COLOUR_NAMES[colour][0],
                "minimum": 0,
                "maximum": max_value,
                "propertyOrder": order,
            }
            schema["translations"]["ru"][COLOUR_NAMES[colour][0]] = COLOUR_NAMES[colour][1]
            schema["properties"][self.property_name]["items"]["required"].append(colour.value)
            order += 1
        return schema


class Type8Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()

        self._current_colour_type: Optional[ColourType] = None
        self._colour_type_lock = asyncio.Lock()

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        async with self._colour_type_lock:
            self._current_colour_type = await self._read_current_colour_type(driver, short_address)
        parameters = [
            ColourState(
                SettingsParamName("Current colour", "Текущий цвет"),
                "current_colour",
                QueryActualLevel,
                Activate,
                ACTUAL_LEVEL_COLOUR_TAGS,
                800,
                self._current_colour_type,
                read_after_save=False,
            ),
            ColourState(
                SettingsParamName("Power On Colour", "Цвет после включения питания"),
                "power_on_colour",
                QueryPowerOnLevel,
                SetPowerOnLevel,
                REPORT_COLOUR_TAGS,
                21,
                self._current_colour_type,
            ),
            ColourState(
                SettingsParamName("System Failure Colour", "Цвет при сбое"),
                "system_failure_colour",
                QuerySystemFailureLevel,
                SetSystemFailureLevel,
                REPORT_COLOUR_TAGS,
                31,
                self._current_colour_type,
            ),
            ScenesSettings(self._current_colour_type),
        ]
        self._parameters = parameters
        return await super().read(driver, short_address)

    async def read_mandatory_info(self, driver: WBDALIDriver, short_address: int) -> None:
        async with self._colour_type_lock:
            if self._current_colour_type is None:
                self._current_colour_type = await self._read_current_colour_type(driver, short_address)

    def get_mqtt_controls(self) -> list[MqttControlBase]:

        if self._current_colour_type == ColourType.RGBWAF:

            def _set_rgb_commands_builder(short_address: int, value: str) -> list[command.Command]:
                components = value.split(";")
                if len(components) != 3:
                    raise ValueError("RGB value must be in format 'R;G;B'")
                try:
                    red, green, blue = (int(c) for c in components)
                except ValueError as e:
                    raise ValueError("RGB components must be integers") from e
                return set_rgb_commands_builder(GearShort(short_address), red, green, blue) + [
                    DTR0(255),
                    Activate(GearShort(short_address)),
                ]

            def _set_white_commands_builder(short_address: int, value: str) -> list[command.Command]:
                try:
                    white = int(value)
                except ValueError as e:
                    raise ValueError("white component must be integer") from e
                return set_waf_commands_builder(GearShort(short_address), white, MASK, MASK) + [
                    DTR0(255),
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
                        "255;255;255",
                    ),
                    commands_builder=_set_rgb_commands_builder,
                ),
                MqttControl(
                    ControlInfo(
                        "set_white",
                        ControlMeta("range", "Wanted White", minimum=0, maximum=255),
                        "255",
                    ),
                    commands_builder=_set_white_commands_builder,
                ),
            ]

        if self._current_colour_type == ColourType.COLOUR_TEMPERATURE:

            def _set_colour_temperature_commands_builder(
                short_address: int, value: str
            ) -> list[command.Command]:
                try:
                    tc = int(value)
                except ValueError as e:
                    raise ValueError("colour temperature must be integer") from e
                return set_colour_temperature_commands_builder(GearShort(short_address), tc) + [
                    DTR0(255),
                    Activate(GearShort(short_address)),
                ]

            return [
                MqttControl(
                    ControlInfo(
                        "current_colour_temperature",
                        ControlMeta(title="Colour Temperature", read_only=True),
                        "0",
                    ),
                ),
                MqttControl(
                    ControlInfo(
                        "colour_temperature_step_warmer",
                        ControlMeta("pushbutton", "Colour Temperature Step Warmer"),
                        "0",
                    ),
                    commands_builder=lambda short_address, _: [
                        ColourTemperatureTcStepWarmer(GearShort(short_address))
                    ],
                ),
                MqttControl(
                    ControlInfo(
                        "colour_temperature_step_cooler",
                        ControlMeta("pushbutton", "Colour Temperature Step Cooler"),
                        "0",
                    ),
                    commands_builder=lambda short_address, _: [
                        ColourTemperatureTcStepCooler(GearShort(short_address))
                    ],
                ),
                MqttControl(
                    ControlInfo(
                        "set_colour_temperature",
                        ControlMeta("range", "Wanted Colour Temperature", minimum=0, maximum=65535),
                        "4000",
                    ),
                    commands_builder=_set_colour_temperature_commands_builder,
                ),
            ]

        if self._current_colour_type == ColourType.PRIMARY_N:

            def _set_primary_n_commands_builder(
                short_address: int, value: str, index: int
            ) -> list[command.Command]:
                try:
                    primary_n = int(value)
                except ValueError as e:
                    raise ValueError(f"primary N{index} must be integer") from e
                return set_primary_n_commands_builder(GearShort(short_address), primary_n, index) + [
                    DTR0(255),
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
                            ControlMeta("range", f"Wanted Primary N{i}", minimum=0, maximum=65535),
                            "32768",
                        ),
                        commands_builder=lambda short_address, value, index=i: _set_primary_n_commands_builder(
                            short_address, value, index
                        ),
                    )
                )
            return res

        if self._current_colour_type == ColourType.XY:

            def _set_x_coordinate_commands_builder(short_address: int, value: str) -> list[command.Command]:
                try:
                    x_coordinate = int(value)
                except ValueError as e:
                    raise ValueError("X coordinate must be integer") from e
                return set_x_coordinate_commands_builder(GearShort(short_address), x_coordinate) + [
                    DTR0(255),
                    Activate(GearShort(short_address)),
                ]

            def _set_y_coordinate_commands_builder(short_address: int, value: str) -> list[command.Command]:
                try:
                    y_coordinate = int(value)
                except ValueError as e:
                    raise ValueError("Y coordinate must be integer") from e
                return set_y_coordinate_commands_builder(GearShort(short_address), y_coordinate) + [
                    DTR0(255),
                    Activate(GearShort(short_address)),
                ]

            return [
                MqttControl(
                    ControlInfo(
                        "current_x_coordinate",
                        ControlMeta(title="Current X Coordinate", read_only=True),
                        "0",
                    ),
                ),
                MqttControl(
                    ControlInfo(
                        "current_y_coordinate",
                        ControlMeta(title="Current Y Coordinate", read_only=True),
                        "0",
                    ),
                ),
                MqttControl(
                    ControlInfo(
                        "x_coordinate_step_up",
                        ControlMeta("pushbutton", "X Coordinate Step Up"),
                        "0",
                    ),
                    commands_builder=lambda short_address, _: [XCoordinateStepUp(GearShort(short_address))],
                ),
                MqttControl(
                    ControlInfo(
                        "x_coordinate_step_down",
                        ControlMeta("pushbutton", "X Coordinate Step Down"),
                        "0",
                    ),
                    commands_builder=lambda short_address, _: [XCoordinateStepDown(GearShort(short_address))],
                ),
                MqttControl(
                    ControlInfo(
                        "y_coordinate_step_up",
                        ControlMeta("pushbutton", "Y Coordinate Step Up"),
                        "0",
                    ),
                    commands_builder=lambda short_address, _: [YCoordinateStepUp(GearShort(short_address))],
                ),
                MqttControl(
                    ControlInfo(
                        "y_coordinate_step_down",
                        ControlMeta("pushbutton", "Y Coordinate Step Down"),
                        "0",
                    ),
                    commands_builder=lambda short_address, _: [YCoordinateStepDown(GearShort(short_address))],
                ),
                MqttControl(
                    ControlInfo(
                        "set_x_coordinate",
                        ControlMeta("range", "Wanted X Coordinate", minimum=0, maximum=65535),
                        "32768",
                    ),
                    commands_builder=_set_x_coordinate_commands_builder,
                ),
                MqttControl(
                    ControlInfo(
                        "set_y_coordinate",
                        ControlMeta("range", "Wanted Y Coordinate", minimum=0, maximum=65535),
                        "32768",
                    ),
                    commands_builder=_set_y_coordinate_commands_builder,
                ),
            ]

        return []

    async def poll_controls(self, driver: WBDALIDriver, short_address: int) -> list[ControlPollResult]:
        """
        Poll controls that require multiple commands to get their value, like current colour.
        Return a list of ControlPollResult objects, e.g. [ControlPollResult("current_rgb", "1;2;3")].
        """

        if self._current_colour_type is None:
            return []
        address = GearShort(short_address)
        set_error = False
        try:
            resp = await driver.run_sequence(
                query_colour_with_level(
                    address, QueryActualLevel(address), ACTUAL_LEVEL_COLOUR_TAGS, self._current_colour_type
                )
            )
        except Exception:
            set_error = True
        if not set_error and resp.colour_type != self._current_colour_type:
            set_error = True
        if self._current_colour_type == ColourType.RGBWAF:
            return [
                ControlPollResult(
                    "current_rgb",
                    (
                        None
                        if set_error
                        else ";".join(
                            str(getattr(resp.colour, colour)) for colour in ["red", "green", "blue"]
                        )
                    ),
                    error="r" if set_error else None,
                ),
                ControlPollResult(
                    "current_white",
                    None if set_error else str(resp.colour.white),
                    error="r" if set_error else None,
                ),
            ]

        if self._current_colour_type == ColourType.COLOUR_TEMPERATURE:
            return [
                ControlPollResult(
                    "current_colour_temperature",
                    None if set_error else str(resp.colour.tc),
                    error="r" if set_error else None,
                )
            ]

        if self._current_colour_type == ColourType.PRIMARY_N:
            return [
                ControlPollResult(
                    f"current_primary_n{i}",
                    None if set_error else str(getattr(resp.colour, f"primary_n{i}")),
                    error="r" if set_error else None,
                )
                for i, _ in enumerate(PRIMARY_N_COLOUR_COMPONENTS)
            ]

        if self._current_colour_type == ColourType.XY:
            return [
                ControlPollResult(
                    "current_x_coordinate",
                    None if set_error else str(resp.colour.x_coordinate),
                    error="r" if set_error else None,
                ),
                ControlPollResult(
                    "current_y_coordinate",
                    None if set_error else str(resp.colour.y_coordinate),
                    error="r" if set_error else None,
                ),
            ]

        return []

    async def _read_current_colour_type(self, driver: WBDALIDriver, short_address: int) -> ColourType:
        res = await query_response(driver, QueryColourStatus(GearShort(short_address)))
        if getattr(res, "colour_type_xy_active") is True:
            return ColourType.XY
        if getattr(res, "colour_type_colour_temperature_Tc_active") is True:
            return ColourType.COLOUR_TEMPERATURE
        if getattr(res, "colour_type_primary_N_active") is True:
            return ColourType.PRIMARY_N
        return ColourType.RGBWAF
