# Type 8 Colour control

import asyncio
import enum
import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Generator, List, Optional, Union

from dali import command
from dali.address import Address, GearShort
from dali.gear.colour import (
    Activate,
    QueryColourStatus,
    QueryColourValue,
    QueryColourValueDTR,
)
from dali.gear.general import (
    DTR0,
    QueryActualLevel,
    QueryContentDTR0,
    QueryPowerOnLevel,
    QuerySceneLevel,
    QuerySystemFailureLevel,
    SetPowerOnLevel,
    SetScene,
    SetSystemFailureLevel,
)

from . import dali_type8_primary_n, dali_type8_rgbwaf, dali_type8_tc, dali_type8_xy
from .common_dali_device import ControlPollResult, MqttControlBase, PropertyStartOrder
from .dali_common_parameters import SCENES_TOTAL
from .dali_parameters import TypeParameters
from .dali_type8_common import ColourComponent
from .dali_type8_tc import TcLimitsSettings, Type8TcLimits
from .settings import SettingsParamBase, SettingsParamName
from .utils import merge_json_schema_properties, merge_translations
from .wbdali import WBDALIDriver
from .wbdali_utils import (
    MASK,
    check_query_response,
    is_broadcast_or_group_address,
    is_transmission_error_response,
    query_response,
    send_commands_with_retry,
)

# pylint: disable=duplicate-code


class ColourType(enum.Enum):
    XY = 0x10
    COLOUR_TEMPERATURE = 0x20
    PRIMARY_N = 0x40
    RGBWAF = 0x80


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


def is_valid_colour_query_response(cmd_item: command.Command, response: Optional[command.Response]) -> bool:
    if is_transmission_error_response(response):
        return False
    if getattr(cmd_item, "response", None) is not None:
        try:
            check_query_response(response)
        except RuntimeError:
            return False
    return True


@dataclass
class ColourSettings:
    colour_type: ColourType
    colour: Union[
        dali_type8_rgbwaf.RgbwafColourValues,
        dali_type8_tc.ColourTemperatureValue,
        dali_type8_primary_n.PrimaryNColourValues,
        dali_type8_xy.XYColourValues,
    ]
    level: int

    def __init__(self, colour_type: ColourType, level: int = MASK) -> None:
        self.colour_type = colour_type
        self.level = level
        if colour_type == ColourType.RGBWAF:
            self.colour = dali_type8_rgbwaf.RgbwafColourValues()
        elif colour_type == ColourType.COLOUR_TEMPERATURE:
            self.colour = dali_type8_tc.ColourTemperatureValue()
        elif colour_type == ColourType.PRIMARY_N:
            self.colour = dali_type8_primary_n.PrimaryNColourValues()
        elif colour_type == ColourType.XY:
            self.colour = dali_type8_xy.XYColourValues()
        else:
            raise RuntimeError(f"Unsupported colour type: {colour_type}")

    def to_json(self) -> dict:
        res = self.colour.to_json()
        res["level"] = self.level
        return res


def query_colour_with_level(  # pylint: disable=too-many-locals
    address: GearShort,
    cmd: command.Command,
    colour_tags: dict[ColourComponent, QueryColourValueDTR],
    default_colour_type: ColourType,
) -> Generator[
    Union[command.Command, List[command.Command]],
    List[Optional[command.Response]],
    ColourSettings,
]:
    first_batch = [cmd, DTR0(QueryColourValueDTR.ReportColourType), QueryColourValue(address)]
    for _ in range(3):
        resp = yield first_batch
        if all(
            is_valid_colour_query_response(command_item, response)
            for command_item, response in zip(first_batch, resp)
        ):  # pylint: disable=too-many-locals
            break
    else:
        raise RuntimeError(f"Failed to get {cmd}: transmission error")

    level = resp[0].raw_value.as_integer
    colour_type = resp[-1].raw_value.as_integer
    # Colour type is set to MASK, so all colours are also MASK
    if MASK == colour_type:
        return ColourSettings(default_colour_type, level)

    res = ColourSettings(ColourType(colour_type), level)

    pending_components = list(res.colour.components)
    last_error = "unknown error"
    for _ in range(3):
        commands = []
        for colour_component in pending_components:
            commands.append(DTR0(colour_tags[colour_component]))
            commands.append(QueryColourValue(address))
            if res.colour_type != ColourType.RGBWAF:
                commands.append(QueryContentDTR0(address))

        responses = yield commands
        response_index = 0
        next_pending_components = []

        for colour_component in pending_components:
            component_commands = [DTR0(colour_tags[colour_component]), QueryColourValue(address)]
            if res.colour_type != ColourType.RGBWAF:
                component_commands.append(QueryContentDTR0(address))
            component_responses = responses[response_index : response_index + len(component_commands)]
            response_index += len(component_commands)

            if not all(
                is_valid_colour_query_response(command_item, response)
                for command_item, response in zip(component_commands, component_responses)
            ):
                last_error = "invalid response"
                next_pending_components.append(colour_component)
                continue

            msb_item = component_responses[1]
            value = msb_item.raw_value.as_integer
            if res.colour_type != ColourType.RGBWAF:
                lsb_item = component_responses[2]
                value = (value << 8) | lsb_item.raw_value.as_integer
            setattr(res.colour, colour_component.value, value)

        if not next_pending_components:
            return res
        pending_components = next_pending_components

    pending_names = [component.value for component in pending_components]
    raise RuntimeError(
        f"Failed to get colour components for {cmd}: {pending_names}; last error: {last_error}"
    )


class ColourState(SettingsParamBase):  # pylint: disable=too-many-instance-attributes
    def __init__(  # pylint: disable=too-many-arguments, R0917
        self,
        name: SettingsParamName,
        property_name: str,
        query_command_class: Callable[[Address], command.Command],
        setup_command_class: Callable[[Address], command.Command],
        colour_tags: dict[ColourComponent, QueryColourValueDTR],
        property_order: int,
        default_colour_type: ColourType,
        limits: Type8TcLimits,
        read_after_save: bool = True,
    ) -> None:
        super().__init__(name)
        self.property_name = property_name
        self.value = ColourSettings(default_colour_type)
        self._query_command_class = query_command_class
        self._setup_command_class = setup_command_class
        self._property_order = property_order
        self._colour_tags = colour_tags
        self._read_after_save = read_after_save
        self._default_colour_type = default_colour_type
        self._limits = limits

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        return await self._read_impl(driver, short_address)

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self.property_name not in value:
            return {}
        values = value.get(self.property_name, {})
        new_state = deepcopy(self.value)
        new_state.colour.from_json(values)
        new_state.level = values.get("level", self.value.level)
        is_for_single_device = not is_broadcast_or_group_address(short_address)
        if is_for_single_device and new_state == self.value:
            return {}
        cmds = new_state.colour.get_write_commands(short_address)
        cmds.extend([DTR0(new_state.level), self._setup_command_class(short_address)])
        await send_commands_with_retry(driver, cmds, logger)
        if is_for_single_device and self._read_after_save:
            return await self._read_impl(driver, short_address)
        self.value = new_state
        return {self.property_name: new_state.to_json()}

    def has_changes(self, new_params: dict) -> bool:
        return self.property_name in new_params

    def get_schema(self, group_and_broadcast: bool) -> dict:
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
                            "propertyOrder": 1,
                            "minimum": 0,
                            "maximum": MASK,
                            "default": MASK,
                            "options": {
                                "grid_columns": 1,
                            },
                        }
                    },
                    "options": {
                        "wb": {
                            "show_editor": True,
                        },
                    },
                    "propertyOrder": self._property_order,
                    "required": ["level"],
                },
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "Light level": "Яркость",
                },
            },
        }
        colour_schema = self.value.colour.get_schema(self._limits)
        root_property = schema["properties"][self.property_name]
        merge_json_schema_properties(root_property, colour_schema)
        merge_translations(schema, colour_schema)
        return schema

    async def _read_impl(self, driver: WBDALIDriver, short_address: Address) -> dict:
        resp = await driver.run_sequence(
            query_colour_with_level(
                short_address,
                self._query_command_class(short_address),
                self._colour_tags,
                self._default_colour_type,
            )
        )
        if resp is None:
            raise RuntimeError(f"Error reading {self.name.en}")
        self.value = resp
        return {self.property_name: resp.to_json()}


class SceneSettings(ColourState):
    def __init__(self, scene_number: int, default_colour_type: ColourType, limits: Type8TcLimits) -> None:
        super().__init__(
            SettingsParamName(f"Scene {scene_number}", f"Сцена {scene_number}"),
            f"scene_{scene_number}",
            lambda address: QuerySceneLevel(address, scene_number),
            lambda address: SetScene(address, scene_number),
            REPORT_COLOUR_TAGS,
            PropertyStartOrder.SCENES.value,
            default_colour_type,
            limits,
        )
        self._scene_number = scene_number

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        return self._to_json(await super().read(driver, short_address, logger))

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        values_to_set = deepcopy(value)
        if value.get("enabled", True):
            values_to_set["level"] = value.get("level", 0)
        else:
            values_to_set["level"] = MASK
        res = await super().write(driver, short_address, {self.property_name: values_to_set}, logger=logger)
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
    def __init__(self, default_colour_type: ColourType, limits: Type8TcLimits) -> None:
        super().__init__(SettingsParamName("Scenes", "Сцены"))

        self.property_name = f"scenes_{default_colour_type.value}"
        self._scenes = [SceneSettings(i, default_colour_type, limits) for i in range(SCENES_TOTAL)]
        self._scene_values = [{} for _ in range(SCENES_TOTAL)]

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        self._scene_values = await asyncio.gather(
            *[scene.read(driver, short_address, logger) for scene in self._scenes]
        )
        return {self.property_name: self._scene_values}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        if self._scenes[0].value is None:
            raise RuntimeError("Cannot write scenes before reading them")
        if self.property_name not in value:
            return {}
        results = await asyncio.gather(
            *[
                scene.write(driver, short_address, scene_value, logger)
                for scene, scene_value in zip(self._scenes, value.get(self.property_name, []))
            ]
        )
        for i, res in enumerate(results):
            self._scene_values[i].update(res)
        return {self.property_name: self._scene_values}

    def has_changes(self, new_params: dict) -> bool:
        return self.property_name in new_params

    def get_schema(self, group_and_broadcast: bool) -> dict:
        item_schema = self._scenes[0].get_schema(group_and_broadcast)
        enabled_schema = {
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "title": "Part of the scene",
                    "propertyOrder": 0,
                    "format": "switch",
                    "options": {
                        "compact": True,
                        "grid_columns": 2,
                    },
                },
            },
            "required": ["enabled"],
        }
        merge_json_schema_properties(item_schema["properties"][self._scenes[0].property_name], enabled_schema)
        schema = {
            "properties": {
                self.property_name: {
                    "type": "array",
                    "title": self.name.en,
                    "format": "table",
                    "minItems": SCENES_TOTAL,
                    "maxItems": SCENES_TOTAL,
                    "items": item_schema["properties"][self._scenes[0].property_name],
                    "propertyOrder": PropertyStartOrder.SCENES.value,
                },
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "Part of the scene": "Часть сцены",
                }
            },
            "required": [self.property_name],
        }
        merge_translations(schema, item_schema)
        return schema


class ColourGroupScenesSettings(ColourState):
    def __init__(self, default_colour_type: ColourType, limits: Type8TcLimits) -> None:
        super().__init__(
            SettingsParamName("Scenes", "Сцены"),
            f"scene_{default_colour_type.value}",
            QuerySceneLevel,
            SetScene,
            REPORT_COLOUR_TAGS,
            PropertyStartOrder.SCENES.value,
            default_colour_type,
            limits,
        )

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        scene_data = value.get(self.property_name)
        if scene_data is None:
            return {}
        index = scene_data.get("index", 0)
        self._setup_command_class = lambda address: SetScene(address, index)
        values_to_set = deepcopy(scene_data)
        if scene_data.get("enabled", True):
            values_to_set["level"] = scene_data.get("level", 0)
        else:
            values_to_set["level"] = MASK
        await super().write(driver, short_address, {self.property_name: values_to_set}, logger)
        return {}

    def get_schema(self, group_and_broadcast: bool) -> dict:
        schema = super().get_schema(True)
        additional_schema = {
            "properties": {
                "index": {
                    "type": "number",
                    "title": "Scene number",
                    "propertyOrder": -1,
                    "enum": list(range(SCENES_TOTAL)),
                    "default": 0,
                },
                "enabled": {
                    "type": "boolean",
                    "title": "Part of the scene",
                    "propertyOrder": 0,
                    "format": "switch",
                    "default": False,
                },
            },
            "required": ["index", "enabled"],
            "translations": {
                "ru": {
                    "Part of the scene": "Часть сцены",
                    "Scene number": "Номер сцены",
                }
            },
        }
        merge_json_schema_properties(schema["properties"][self.property_name], additional_schema)
        merge_translations(schema, additional_schema)
        return schema


class CurrentColourState(ColourState):
    def __init__(self, default_colour_type: ColourType, limits: Type8TcLimits) -> None:
        super().__init__(
            SettingsParamName("Current colour", "Текущий цвет"),
            f"current_colour_{default_colour_type.value}",
            QueryActualLevel,
            Activate,
            ACTUAL_LEVEL_COLOUR_TAGS,
            PropertyStartOrder.SYSTEM_FAILURE_LEVEL.value + 1,
            default_colour_type,
            limits,
            read_after_save=False,
        )


class PowerOnColourState(ColourState):
    def __init__(self, default_colour_type: ColourType, limits: Type8TcLimits) -> None:
        super().__init__(
            SettingsParamName("Power On Colour", "Состояние после включения питания"),
            f"power_on_colour_{default_colour_type.value}",
            QueryPowerOnLevel,
            SetPowerOnLevel,
            REPORT_COLOUR_TAGS,
            PropertyStartOrder.POWER_ON_LEVEL.value,
            default_colour_type,
            limits,
        )


class SystemFailureColourState(ColourState):
    def __init__(self, default_colour_type: ColourType, limits: Type8TcLimits) -> None:
        super().__init__(
            SettingsParamName("System Failure Colour", "Состояние после сбоя"),
            f"system_failure_colour_{default_colour_type.value}",
            QuerySystemFailureLevel,
            SetSystemFailureLevel,
            REPORT_COLOUR_TAGS,
            PropertyStartOrder.SYSTEM_FAILURE_LEVEL.value,
            default_colour_type,
            limits,
        )


class Type8Parameters(TypeParameters):
    def __init__(self) -> None:
        super().__init__()

        self._current_colour_type: Optional[ColourType] = None
        self._limits = Type8TcLimits()
        self._colour_type_lock = asyncio.Lock()

    @property
    def default_colour_type(self) -> ColourType:
        return self._current_colour_type if self._current_colour_type is not None else ColourType.RGBWAF

    @property
    def tc_limits(self) -> Type8TcLimits:
        return self._limits

    async def read_mandatory_info(
        self,
        driver: WBDALIDriver,
        short_address: GearShort,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        async with self._colour_type_lock:
            if self._current_colour_type is None:
                self._current_colour_type = await self._read_current_colour_type(
                    driver,
                    short_address,
                    logger,
                )
                if self._current_colour_type == ColourType.COLOUR_TEMPERATURE:
                    result = await dali_type8_tc.read_colour_temperature_limits_mirek(
                        driver,
                        short_address,
                        logger,
                    )
                    self._limits.update_from(result)
        parameters: list[SettingsParamBase] = [
            CurrentColourState(self.default_colour_type, self._limits),
            PowerOnColourState(self.default_colour_type, self._limits),
            SystemFailureColourState(self.default_colour_type, self._limits),
            ScenesSettings(self.default_colour_type, self._limits),
        ]
        if self._current_colour_type == ColourType.COLOUR_TEMPERATURE:
            parameters.append(TcLimitsSettings(self._limits))
        self._parameters = parameters

    def get_mqtt_controls(self) -> list[MqttControlBase]:
        if self._current_colour_type == ColourType.RGBWAF:
            return dali_type8_rgbwaf.get_mqtt_controls(only_setup_controls=False)
        if self._current_colour_type == ColourType.COLOUR_TEMPERATURE:
            return dali_type8_tc.get_mqtt_controls(self._limits.tc_min_mirek, self._limits.tc_max_mirek)
        if self._current_colour_type == ColourType.PRIMARY_N:
            return dali_type8_primary_n.get_mqtt_controls()
        if self._current_colour_type == ColourType.XY:
            return dali_type8_xy.get_mqtt_controls()
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
        except Exception:  # pylint: disable=broad-exception-caught
            set_error = True
        if not set_error and resp.colour_type != self._current_colour_type:
            set_error = True
        if self._current_colour_type == ColourType.RGBWAF:
            return dali_type8_rgbwaf.handle_poll_controls_result(None if set_error else resp.colour)
        if self._current_colour_type == ColourType.COLOUR_TEMPERATURE:
            return dali_type8_tc.handle_poll_controls_result(None if set_error else resp.colour)
        if self._current_colour_type == ColourType.PRIMARY_N:
            return dali_type8_primary_n.handle_poll_controls_result(None if set_error else resp.colour)
        if self._current_colour_type == ColourType.XY:
            return dali_type8_xy.handle_poll_controls_result(None if set_error else resp.colour)

        return []

    def get_group_parameters(self) -> list[SettingsParamBase]:
        params: list[SettingsParamBase] = [
            PowerOnColourState(self.default_colour_type, self._limits),
            SystemFailureColourState(self.default_colour_type, self._limits),
            ColourGroupScenesSettings(self.default_colour_type, self._limits),
        ]
        if self._current_colour_type == ColourType.COLOUR_TEMPERATURE:
            params.append(TcLimitsSettings(self._limits))
        return params

    async def _read_current_colour_type(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        logger: Optional[logging.Logger] = None,
    ) -> ColourType:
        res = await query_response(driver, QueryColourStatus(short_address), logger)
        if getattr(res, "colour_type_xy_active") is True:
            return ColourType.XY
        if getattr(res, "colour_type_colour_temperature_Tc_active") is True:
            return ColourType.COLOUR_TEMPERATURE
        if getattr(res, "colour_type_primary_N_active") is True:
            return ColourType.PRIMARY_N
        return ColourType.RGBWAF
