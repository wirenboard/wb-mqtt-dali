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
    QueryColourStatus,
    QueryColourValue,
    QueryColourValueDTR,
    SetTemporaryColourTemperature,
    SetTemporaryPrimaryNDimLevel,
    SetTemporaryRGBDimLevel,
    SetTemporaryWAFDimLevel,
    SetTemporaryXCoordinate,
    SetTemporaryYCoordinate,
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

from .common_gear_parameters import SCENES_TOTAL
from .extended_gear_parameters import TypeParameters
from .settings import SettingsParamAddress, SettingsParamBase, SettingsParamName
from .wbdali import MASK, WBDALIDriver, query_request


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
        return [
            DTR0(self.red),
            DTR1(self.green),
            DTR2(self.blue),
            SetTemporaryRGBDimLevel(address),
            DTR0(self.white),
            DTR1(self.amber),
            DTR2(self.free_colour),
            SetTemporaryWAFDimLevel(address),
        ]


@dataclass
class ColourTemperatureValue:
    tc: int = MASK
    components = COLOR_TEMPERATURE_COLOUR_COMPONENTS

    def get_write_commands(self, address: GearShort) -> List[command.Command]:
        return [
            DTR0(self.tc),
            SetTemporaryColourTemperature(address),
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
            res.extend(
                [
                    DTR0((value >> 16) & 0xFF),
                    DTR1((value & 0xFF)),
                    DTR2(index),
                    SetTemporaryPrimaryNDimLevel(address),
                ]
            )
        return res


@dataclass
class XYColourValues:
    x_coordinate: int = MASK
    y_coordinate: int = MASK
    components = XY_COLOUR_COMPONENTS

    def get_write_commands(self, address: GearShort) -> List[command.Command]:
        res = [
            DTR0((self.x_coordinate >> 16) & 0xFF),
            DTR1((self.x_coordinate & 0xFF)),
            SetTemporaryXCoordinate(address),
            DTR0((self.y_coordinate >> 16) & 0xFF),
            DTR1((self.y_coordinate & 0xFF)),
            SetTemporaryYCoordinate(address),
        ]
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
    # Colour type is set to MASK, so all colour are also MASK
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

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        if not isinstance(address, GearShort):
            raise ValueError("Address must be a GearShort")
        resp = await driver.run_sequence(
            query_colour_with_level(
                address, self._query_command_class(address), self._colour_tags, self._default_colour_type
            )
        )
        if resp is None:
            raise RuntimeError(f"Error reading {self.name.en}")
        self.value = resp
        res = asdict(resp.colour)
        res["level"] = resp.level
        return {self.property_name: res}

    async def write(self, driver: WBDALIDriver, address: SettingsParamAddress, value: dict) -> dict:
        if self.property_name not in value:
            return {}
        if not isinstance(address, GearShort):
            raise ValueError("Address must be a GearShort")
        if self.value is None or self.value.colour is None:
            raise RuntimeError(f"Cannot write {self.name.en} before reading it")
        values = value.get(self.property_name, {})
        new_state = deepcopy(self.value)
        for colour in new_state.colour.components:
            if colour.value in values:
                setattr(new_state.colour, colour.value, values[colour.value])
        new_state.level = values.get("level", self.value.level)
        if new_state == self.value:
            return {}
        cmds = new_state.colour.get_write_commands(address)
        cmds.extend([DTR0(new_state.level), self._setup_command_class(address)])
        await driver.send_commands(cmds)
        if self._read_after_save:
            return await self.read(driver, address)
        self.value = new_state
        res = asdict(new_state.colour)
        res["level"] = new_state.level
        return {self.property_name: res}

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
            max_value = get_max_colour_value(self.value.colour_type)
            for index, colour in enumerate(self.value.colour.components):
                schema["properties"][self.property_name]["properties"][colour.value] = {
                    "type": "integer",
                    "title": COLOUR_NAMES[colour][0],
                    "minimum": 0,
                    "maximum": max_value,
                    "propertyOrder": index + 1,
                    "options": {
                        "grid_columns": 2,
                    },
                }
                schema["properties"][self.property_name]["required"].append(colour.value)
        return schema


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

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        res = await super().read(driver, address)
        value = res[self.property_name]
        value["enabled"] = value.get("level") != MASK
        return value

    async def write(self, driver: WBDALIDriver, address: SettingsParamAddress, value: dict) -> dict:
        values_to_set = deepcopy(value)
        if value.get("enabled", True):
            values_to_set["level"] = value.get("level", 0)
        else:
            values_to_set["level"] = MASK
        return await super().write(driver, address, {self.property_name: values_to_set})


class ScenesSettings(SettingsParamBase):
    def __init__(self, default_colour_type: ColourType) -> None:
        super().__init__(SettingsParamName("Scenes", "Сцены"))

        self.property_name = "scenes"
        self._scenes = [SceneSettings(i, default_colour_type) for i in range(SCENES_TOTAL)]
        self._scene_values = [{} for _ in range(SCENES_TOTAL)]

    async def read(self, driver: WBDALIDriver, address: SettingsParamAddress) -> dict:
        if not isinstance(address, GearShort):
            raise ValueError("Address must be a GearShort")
        self._scene_values = await asyncio.gather(*[scene.read(driver, address) for scene in self._scenes])
        return {self.property_name: self._scene_values}

    async def write(self, driver: WBDALIDriver, address: SettingsParamAddress, value: dict) -> dict:
        if self._scenes[0].value is None:
            raise RuntimeError("Cannot write scenes before reading them")
        if self.property_name not in value:
            return {}
        if not isinstance(address, GearShort):
            raise ValueError("Address must be a GearShort")
        results = await asyncio.gather(
            *[
                scene.write(driver, address, scene_value)
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
                                "propertyOrder": 2,
                                "minimum": 0,
                                "maximum": 255,
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

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        res = await query_request(driver, QueryColourStatus(address))
        default_colour_type = ColourType.RGBWAF
        if (res >> 4) & 0x01 == 1:
            default_colour_type = ColourType.XY
        elif (res >> 5) & 0x01 == 1:
            default_colour_type = ColourType.COLOUR_TEMPERATURE
        elif (res >> 6) & 0x01 == 1:
            default_colour_type = ColourType.PRIMARY_N
        parameters = [
            ColourState(
                SettingsParamName("Current colour", "Текущий цвет"),
                "current_colour",
                QueryActualLevel,
                Activate,
                ACTUAL_LEVEL_COLOUR_TAGS,
                800,
                default_colour_type,
                read_after_save=False,
            ),
            ColourState(
                SettingsParamName("Power On Colour", "Цвет после включения питания"),
                "power_on_colour",
                QueryPowerOnLevel,
                SetPowerOnLevel,
                REPORT_COLOUR_TAGS,
                21,
                default_colour_type,
            ),
            ColourState(
                SettingsParamName("System Failure Colour", "Цвет при сбое"),
                "system_failure_colour",
                QuerySystemFailureLevel,
                SetSystemFailureLevel,
                REPORT_COLOUR_TAGS,
                31,
                default_colour_type,
            ),
            ScenesSettings(default_colour_type),
        ]
        self._parameters = parameters
        return await super().read(driver, address)
