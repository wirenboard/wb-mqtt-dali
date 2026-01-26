# Type 8 Colour control

import asyncio
import enum
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Callable, Generator, List, Optional, Union

from dali import command
from dali.address import GearShort
from dali.gear.colour import (
    Activate,
    QueryColourStatus,
    QueryColourValue,
    QueryColourValueDTR,
    SetTemporaryRGBDimLevel,
    SetTemporaryWAFDimLevel,
)
from dali.gear.general import (
    DTR0,
    DTR1,
    DTR2,
    QueryActualLevel,
    QueryPowerOnLevel,
    QuerySceneLevel,
    QuerySystemFailureLevel,
    SetPowerOnLevel,
    SetScene,
    SetSystemFailureLevel,
)

from .common_gear_parameters import SCENES_TOTAL
from .extended_gear_parameters import GearParamBase, GearParamName, TypeParameters
from .wbdali import MASK, WBDALIDriver, query_request


class ColourType(enum.Enum):
    XY = 0
    COLOUR_TEMPERATURE = 1
    PRIMARY_N = 2
    RGBWAF = 3
    Lunatone_RGBW = 128
    MASK = 255


class ColourComponent(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    WHITE = "white"
    AMBER = "amber"
    FREE_COLOUR = "free_colour"


COLOUR_NAMES = {
    ColourComponent.RED: ("Red", "Красный"),
    ColourComponent.GREEN: ("Green", "Зеленый"),
    ColourComponent.BLUE: ("Blue", "Синий"),
    ColourComponent.WHITE: ("White", "Белый"),
    ColourComponent.AMBER: ("Amber", "Янтарный"),
    ColourComponent.FREE_COLOUR: ("Free colour", "Свободный цвет"),
}


@dataclass
class ColourValues:
    red: int = MASK
    green: int = MASK
    blue: int = MASK
    white: int = MASK
    amber: int = MASK
    free_colour: int = MASK


@dataclass
class ColourSettings:
    colour_type: ColourType = ColourType.MASK
    colour: ColourValues = field(default_factory=ColourValues)
    level: int = MASK


ACTUAL_LEVEL_COLOUR_TAGS = {
    ColourComponent.RED: QueryColourValueDTR.RedDimLevel,
    ColourComponent.GREEN: QueryColourValueDTR.GreenDimLevel,
    ColourComponent.BLUE: QueryColourValueDTR.BlueDimLevel,
    ColourComponent.WHITE: QueryColourValueDTR.WhiteDimLevel,
    ColourComponent.AMBER: QueryColourValueDTR.AmberDimLevel,
    ColourComponent.FREE_COLOUR: QueryColourValueDTR.FreecolourDimLevel,
}

REPORT_COLOUR_TAGS = {
    ColourComponent.RED: QueryColourValueDTR.ReportRedDimLevel,
    ColourComponent.GREEN: QueryColourValueDTR.ReportGreenDimLevel,
    ColourComponent.BLUE: QueryColourValueDTR.ReportBlueDimLevel,
    ColourComponent.WHITE: QueryColourValueDTR.ReportWhiteDimLevel,
    ColourComponent.AMBER: QueryColourValueDTR.ReportAmberDimLevel,
    ColourComponent.FREE_COLOUR: QueryColourValueDTR.ReportFreecolourDimLevel,
}


def query_colour_with_level(
    address: GearShort, cmd: command.Command, colour_components: List[tuple[str, QueryColourValueDTR]]
) -> Generator[
    Union[command.Command, List[command.Command]],
    Union[Optional[command.Response], List[Optional[command.Response]]],
    ColourSettings,
]:
    res = ColourSettings()

    resp = yield [cmd, DTR0(QueryColourValueDTR.ReportColourType), QueryColourValue(address)]

    if resp[0] is None or resp[0].raw_value is None:
        raise RuntimeError(f"Failed to get {cmd}")
    res.level = resp[0].raw_value.as_integer

    if resp[-1] is None or resp[-1].raw_value is None:
        raise RuntimeError(f"Failed to get colour type for {cmd}")

    colour_type = resp[-1].raw_value.as_integer
    # Colour type is set to MASK, so all colours are also MASK
    if MASK == colour_type:
        res.colour_type = ColourType.RGBWAF
        return res

    if colour_type not in [ColourType.RGBWAF.value, ColourType.Lunatone_RGBW.value]:
        raise RuntimeError(f"Unsupported colour type for {cmd}: {colour_type}")
    res.colour_type = ColourType.RGBWAF

    cmds = []

    for _colour_name, colour in colour_components:
        cmds.append(DTR0(colour))
        cmds.append(QueryColourValue(address))

    resp = yield cmds

    for i, (colour_name, _colour) in enumerate(colour_components):
        resp_item = resp[2 * i + 1]
        if resp_item is None or resp_item.raw_value is None:
            raise RuntimeError(f"Failed to get {colour_name} for {cmd}")
        setattr(res.colour, colour_name, resp_item.raw_value.as_integer)

    return res


class ColourState(GearParamBase):
    def __init__(
        self,
        name: GearParamName,
        property_name: str,
        query_command_class: Callable[[GearShort], command.Command],
        setup_command_class: Callable[[GearShort], command.Command],
        colour_components: List[ColourComponent],
        colour_tags: dict,
        property_order: int,
    ) -> None:
        super().__init__(name)
        self.property_name = property_name
        self._query_command_class = query_command_class
        self._setup_command_class = setup_command_class
        self._last_value: Optional[ColourSettings] = None
        self._property_order = property_order
        self._colour_components = colour_components
        self._colour_tags = colour_tags

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        colours = [(colour.value, self._colour_tags[colour]) for colour in self._colour_components]
        resp = await driver.run_sequence(
            query_colour_with_level(address, self._query_command_class(address), colours)
        )
        if resp is None:
            raise RuntimeError(f"Error reading {self.name.en}")
        self._last_value = resp
        return {self.property_name: asdict(resp.colour)}

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        if self.property_name not in value:
            return {}
        values_to_set = ColourValues() if self._last_value is None else deepcopy(self._last_value.colour)
        new_values = value.get(self.property_name, {})
        for colour in self._colour_components:
            new_value = new_values.get(colour.value, None)
            if new_value is not None:
                setattr(values_to_set, colour.value, new_value)
        if self._last_value is not None and values_to_set == self._last_value.colour:
            return {}
        cmds = [
            DTR0(values_to_set.red),
            DTR1(values_to_set.green),
            DTR2(values_to_set.blue),
            SetTemporaryRGBDimLevel(address),
            DTR0(values_to_set.white),
            DTR1(values_to_set.amber),
            DTR2(values_to_set.free_colour),
            SetTemporaryWAFDimLevel(address),
            self._setup_command_class(address),
        ]
        await driver.send_commands(cmds)
        return await self.read(driver, address)

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
        schema = {
            "properties": {
                self.property_name: {
                    "type": "object",
                    "title": self.name.en,
                    "properties": {},
                    "propertyOrder": self._property_order,
                    "required": [],
                },
            },
            "translations": {"ru": {self.name.en: self.name.ru}},
        }
        for index, colour in enumerate(self._colour_components):
            schema["properties"][self.property_name]["properties"][colour.value] = {
                "type": "integer",
                "title": COLOUR_NAMES[colour][0],
                "minimum": 0,
                "maximum": 255,
                "propertyOrder": index,
                "options": {
                    "grid_columns": 2,
                },
            }
            schema["properties"][self.property_name]["required"].append(colour.value)
        return schema


class SceneSettings:
    def __init__(
        self,
        scene_number: int,
        colour_components: List[ColourComponent],
    ) -> None:
        self._scene_number = scene_number
        self._colour_components = colour_components
        self._last_value: Optional[ColourSettings] = None

    async def read(self, driver: WBDALIDriver, addr: GearShort) -> dict:
        colours = [(colour.value, REPORT_COLOUR_TAGS[colour]) for colour in self._colour_components]
        resp = await driver.run_sequence(
            query_colour_with_level(addr, QuerySceneLevel(addr, self._scene_number), colours)
        )
        if resp is None:
            raise RuntimeError(f"Error reading scene {self._scene_number}")
        self._last_value = resp
        res = asdict(resp.colour)
        if resp.level == MASK:
            res["enabled"] = False
            res["level"] = 0
        else:
            res["enabled"] = True
            res["level"] = resp.level
        return res

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        values_to_set = ColourSettings() if self._last_value is None else deepcopy(self._last_value)
        values_to_set.colour_type = ColourType.RGBWAF
        if value.get("enabled", True):
            values_to_set.level = value.get("level", values_to_set.level)
        else:
            values_to_set.level = MASK
        for colour in self._colour_components:
            if colour.value in value:
                setattr(values_to_set.colour, colour.value, value[colour.value])
        if self._last_value is not None and values_to_set == self._last_value:
            return {}
        cmds = [
            DTR0(values_to_set.colour.red),
            DTR1(values_to_set.colour.green),
            DTR2(values_to_set.colour.blue),
            SetTemporaryRGBDimLevel(address),
            DTR0(values_to_set.colour.white),
            DTR1(values_to_set.colour.amber),
            DTR2(values_to_set.colour.free_colour),
            SetTemporaryWAFDimLevel(address),
            DTR0(values_to_set.level),
            SetScene(address, self._scene_number),
        ]
        await driver.send_commands(cmds)
        return await self.read(driver, address)


class ScenesSettings(GearParamBase):
    def __init__(self, colour_components: List[ColourComponent]) -> None:
        super().__init__(GearParamName("Scenes", "Сцены"))

        self.property_name = "scenes"

        self._colour_components = colour_components
        self._scenes = [SceneSettings(i, colour_components) for i in range(SCENES_TOTAL)]
        self._scene_values = [{} for _ in range(SCENES_TOTAL)]

    async def read(self, driver: WBDALIDriver, address: GearShort) -> dict:
        self._scene_values = await asyncio.gather(*[scene.read(driver, address) for scene in self._scenes])
        return {self.property_name: self._scene_values}

    async def write(self, driver: WBDALIDriver, address: GearShort, value: dict) -> dict:
        if self.property_name not in value:
            return {}
        results = await asyncio.gather(
            *[
                scene.write(driver, address, scene_value)
                for scene, scene_value in zip(self._scenes, value.get(self.property_name, []))
            ]
        )
        for i, res in enumerate(results):
            self._scene_values[i].update(res)
        return {self.property_name: self._scene_values}

    async def get_schema(self, driver: WBDALIDriver, address: GearShort) -> dict:
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
        for colour in self._colour_components:
            schema["properties"][self.property_name]["items"]["properties"][colour.value] = {
                "type": "integer",
                "title": COLOUR_NAMES[colour][0],
                "minimum": 0,
                "maximum": 255,
                "propertyOrder": order,
            }
            schema["translations"]["ru"][COLOUR_NAMES[colour][0]] = COLOUR_NAMES[colour][1]
            schema["properties"][self.property_name]["items"]["required"].append(colour.value)
            order += 1
        return schema


class Type8Parameters(TypeParameters):
    def __init__(self) -> None:
        self._active_colour_type = None
        super().__init__()

    async def get_parameters(self, driver: WBDALIDriver, address: GearShort) -> list[GearParamBase]:
        parameters = []
        colour_status = await query_request(driver, QueryColourStatus(address))
        if colour_status >> 7 & 0x01 == 1:
            colour_components = [
                ColourComponent.RED,
                ColourComponent.GREEN,
                ColourComponent.BLUE,
                ColourComponent.WHITE,
            ]
            self._active_colour_type = ColourType.RGBWAF
            parameters = [
                ColourState(
                    GearParamName("Current colour", "Текущий цвет"),
                    "current_colour",
                    QueryActualLevel,
                    Activate,
                    colour_components,
                    ACTUAL_LEVEL_COLOUR_TAGS,
                    800,
                ),
                ColourState(
                    GearParamName("Power On Colour", "Цвет после включения питания"),
                    "power_on_colour",
                    QueryPowerOnLevel,
                    SetPowerOnLevel,
                    colour_components,
                    REPORT_COLOUR_TAGS,
                    21,
                ),
                ColourState(
                    GearParamName("System Failure Colour", "Цвет при сбое"),
                    "system_failure_colour",
                    QuerySystemFailureLevel,
                    SetSystemFailureLevel,
                    colour_components,
                    REPORT_COLOUR_TAGS,
                    31,
                ),
                ScenesSettings(colour_components),
            ]

        return parameters
