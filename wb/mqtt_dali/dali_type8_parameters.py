# Type 8 Colour control

import asyncio
import enum
import logging
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field, fields
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
from .common_dali_device import (
    EVENT_RESYNC_BASE_INTERVAL,
    ControlPollResult,
    ControlsPollRequestResult,
    EventPollSchedule,
    MqttControlBase,
    PropertyStartOrder,
)
from .dali_common_parameters import SCENES_TOTAL
from .dali_parameters import TypeParameters
from .dali_type8_common import ColourComponent
from .dali_type8_tc import TcLimitsSettings, Type8TcLimits
from .settings import SettingsParamBase, SettingsParamName
from .utils import merge_json_schema_properties, merge_translations
from .wbdali import FramePriority, WBDALIDriver
from .wbdali_utils import (
    MASK,
    MASK_2BYTES,
    check_query_response,
    is_broadcast_or_group_address,
    is_transmission_error_response,
    query_response,
    send_commands_with_retry,
)

MAX_COLOUR_SUBBATCH_RETRIES = 3

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

COMPONENTS_BY_COLOUR_TYPE: "dict[ColourType, list[ColourComponent]]" = {
    ColourType.RGBWAF: dali_type8_rgbwaf.RGBW_COLOUR_COMPONENTS,
    ColourType.COLOUR_TEMPERATURE: dali_type8_tc.COLOR_TEMPERATURE_COLOUR_COMPONENTS,
    ColourType.PRIMARY_N: dali_type8_primary_n.PRIMARY_N_COLOUR_COMPONENTS,
    ColourType.XY: dali_type8_xy.XY_COLOUR_COMPONENTS,
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
        await send_commands_with_retry(driver, cmds, logger, priority=FramePriority.CONFIGURATION)
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
            ),
            FramePriority.CONFIGURATION,
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
        self._scene_values: list[dict] = [{} for _ in range(SCENES_TOTAL)]
        # Index of the next scene not yet read; 0 = nothing read, SCENES_TOTAL = complete.
        self._fetch_cursor = 0

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        # Always a full read of all 16 scenes (refresh paths rely on this); marks fetch
        # complete (full read => nothing left for the background fetch).
        await self._read_scenes(driver, short_address, range(SCENES_TOTAL), logger)
        self._fetch_cursor = SCENES_TOTAL
        return {self.property_name: self._scene_values}

    async def fetch(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> bool:
        # One scene per call: a scene is a colour sequence on the shared DTR0 and cannot be split.
        if self._fetch_cursor >= SCENES_TOTAL:
            return True
        await self._read_scenes(driver, short_address, [self._fetch_cursor], logger)
        self._fetch_cursor += 1
        return self._fetch_cursor >= SCENES_TOTAL

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

    def scene_colour(self, index: int) -> Optional[ColourSettings]:
        """Colour of scene ``index``, or ``None`` if not yet read or scene disabled (level MASK)."""
        if index < 0 or index >= self._fetch_cursor:
            return None
        colour = self._scenes[index].value
        if colour is None or colour.level == MASK:
            return None
        return colour

    def scene_level(self, index: int) -> Optional[int]:
        colour = self.scene_colour(index)
        return None if colour is None else colour.level

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

    # --- Private ---

    async def _read_scenes(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        scene_indices: Iterable[int],
        logger: Optional[logging.Logger],
    ) -> None:
        indices = list(scene_indices)
        results = await asyncio.gather(
            *[self._scenes[i].read(driver, short_address, logger) for i in indices]
        )
        for i, result in zip(indices, results):
            self._scene_values[i] = result


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


@dataclass
class _Type8ColourReadProgress:
    address: Address
    level: int = MASK
    colour_type: Optional[ColourType] = None
    pending_components: list = field(default_factory=list)
    done_values: dict = field(default_factory=dict)


class Type8Parameters(EventPollSchedule, TypeParameters):  # pylint: disable=too-many-instance-attributes
    def __init__(self) -> None:
        super().__init__()

        self._current_colour_type: Optional[ColourType] = None
        self._limits = Type8TcLimits()
        self._colour_type_lock = asyncio.Lock()

        # Event control: long, jittered re-sync base interval (kept fresh by Activate
        # sniffing + confirmation polls).
        self._init_poll_schedule(EVENT_RESYNC_BASE_INTERVAL, randomize_poll_interval=True)
        self._read_progress: Optional[_Type8ColourReadProgress] = None
        # Typed active-colour state, projected to the current_* topics.
        self._colour_value: Optional[object] = None
        self._scenes_settings: Optional[ScenesSettings] = None

    @property
    def default_colour_type(self) -> ColourType:
        return self._current_colour_type if self._current_colour_type is not None else ColourType.RGBWAF

    @property
    def tc_limits(self) -> Type8TcLimits:
        return self._limits

    @property
    def scenes_settings(self) -> Optional["ScenesSettings"]:
        return self._scenes_settings

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
        self._scenes_settings = ScenesSettings(self.default_colour_type, self._limits)
        parameters: list[SettingsParamBase] = [
            CurrentColourState(self.default_colour_type, self._limits),
            PowerOnColourState(self.default_colour_type, self._limits),
            SystemFailureColourState(self.default_colour_type, self._limits),
            self._scenes_settings,
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

    def is_poll_due(self, now: float, default_poll_interval: float) -> bool:
        if self._current_colour_type is None:
            return False
        # In-progress read keeps the handler eligible for the next round so the
        # remaining subbatches can run regardless of the per-handler interval.
        if self._read_progress is not None:
            return True
        if self.last_poll_time is None:
            return True
        interval = self.poll_interval if self.poll_interval is not None else default_poll_interval
        return now - self.last_poll_time >= interval

    def cancel_pending_poll(self) -> None:
        self._read_progress = None

    def apply_colour(self, components: dict[str, int]) -> list[ControlPollResult]:
        """Project sniffed/own colour components onto the current_* topics (optimistic).

        Overlays the captured components on the active typed colour (a fresh all-MASK
        base before the first real read), clamps Tc to limits, and returns the per-topic
        publishes. Components left at the MASK sentinel — those this command did not set
        and that no prior read filled in — are not published (they would otherwise emit
        the sentinel as a real value); the confirmation poll fills them in. Empty
        components -> poll only.
        """
        ct = self._current_colour_type
        if ct is None or not components:
            return []
        colour = self._colour_value if self._colour_value is not None else ColourSettings(ct).colour
        for key, raw in components.items():
            if hasattr(colour, key):
                setattr(colour, key, raw)
        self._clamp_tc(colour)
        self._colour_value = colour
        return self._known_colour_results(ct, colour)

    def apply_scene_colour(self, scene_colour: "ColourSettings") -> list[ControlPollResult]:
        """Project a DT8 GoToScene's stored scene colour onto the current_* topics.

        A scene stores each colour component or the MASK sentinel meaning "this scene
        leaves the component unchanged". A MASK component must keep the device's current
        value, not overwrite it with the sentinel -- so only the scene's non-MASK
        components are overlaid (via ``apply_colour``, which also drops MASK topics from
        the publish); the rest are left as-is and confirmed by the poll.
        """
        ct = self._current_colour_type
        if ct is None or scene_colour.colour_type != ct:
            return []
        fresh = ColourSettings(ct).colour
        components = {
            member.name: getattr(scene_colour.colour, member.name)
            for member in fields(scene_colour.colour)
            if getattr(scene_colour.colour, member.name) != getattr(fresh, member.name)
        }
        return self.apply_colour(components)

    def has_in_progress_read(self) -> bool:
        return self._read_progress is not None

    def peek_next_subbatch_size(self) -> int:
        if self._current_colour_type is None:
            return 0
        if self._read_progress is None or self._read_progress.colour_type is None:
            return 3
        return 2 if self._read_progress.colour_type == ColourType.RGBWAF else 3

    def next_poll_step(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        driver: WBDALIDriver,
        address: Address,
        max_commands: int,
        default_max_commands: int,
        now: float,
        logger: Optional[logging.Logger] = None,
    ) -> ControlsPollRequestResult:
        del default_max_commands, logger
        if self._current_colour_type is None:
            return ControlsPollRequestResult(has_more=False)
        if self.peek_next_subbatch_size() > max_commands:
            return ControlsPollRequestResult(has_more=True)
        if self._read_progress is None:
            is_first_poll = self.last_poll_time is None
            self._read_progress = _Type8ColourReadProgress(address)
            self.last_poll_time = now
            self._redraw_poll_interval()
            if is_first_poll:
                self._reconfirm_after_first_poll(now)
        progress = self._read_progress

        if progress.colour_type is None:
            return ControlsPollRequestResult(
                has_more=True,
                poll_coroutine=lambda: self._do_first_subbatch(driver, progress),
                commands_count=3,
            )

        is_rgbwaf = progress.colour_type == ColourType.RGBWAF
        commands_count = 2 if is_rgbwaf else 3
        more_after_this = len(progress.pending_components) > 1
        return ControlsPollRequestResult(
            has_more=more_after_this,
            poll_coroutine=lambda: self._do_component_subbatch(driver, progress),
            commands_count=commands_count,
        )

    async def _do_first_subbatch(
        self, driver: WBDALIDriver, progress: _Type8ColourReadProgress
    ) -> list[ControlPollResult]:
        cmds = [
            QueryActualLevel(progress.address),
            DTR0(QueryColourValueDTR.ReportColourType),
            QueryColourValue(progress.address),
        ]
        responses = await self._send_subbatch_with_retries(driver, cmds)
        if responses is None:
            self._read_progress = None
            return self._build_error_results()
        progress.level = responses[0].raw_value.as_integer
        colour_type_raw = responses[-1].raw_value.as_integer
        if colour_type_raw == MASK:
            self._read_progress = None
            return self._build_error_results()
        try:
            colour_type = ColourType(colour_type_raw)
        except ValueError:
            self._read_progress = None
            return self._build_error_results()
        if colour_type != self._current_colour_type:
            self._read_progress = None
            return self._build_error_results()
        progress.colour_type = colour_type
        progress.pending_components = list(COMPONENTS_BY_COLOUR_TYPE[colour_type])
        return []

    async def _do_component_subbatch(
        self, driver: WBDALIDriver, progress: _Type8ColourReadProgress
    ) -> list[ControlPollResult]:
        component = progress.pending_components[0]
        is_rgbwaf = progress.colour_type == ColourType.RGBWAF
        cmds = [
            DTR0(ACTUAL_LEVEL_COLOUR_TAGS[component]),
            QueryColourValue(progress.address),
        ]
        if not is_rgbwaf:
            cmds.append(QueryContentDTR0(progress.address))
        responses = await self._send_subbatch_with_retries(driver, cmds)
        if responses is None:
            self._read_progress = None
            return self._build_error_results()
        msb = responses[1].raw_value.as_integer
        if is_rgbwaf:
            value = msb
        else:
            lsb = responses[2].raw_value.as_integer
            value = (msb << 8) | lsb
        progress.done_values[component.value] = value
        progress.pending_components.pop(0)
        if progress.pending_components:
            return []
        results = self._build_success_results(progress)
        self._read_progress = None
        return results

    @staticmethod
    async def _send_subbatch_with_retries(driver: WBDALIDriver, cmds: list) -> Optional[list]:
        responses = None
        for _ in range(MAX_COLOUR_SUBBATCH_RETRIES):
            try:
                responses = await driver.send_commands(cmds, priority=FramePriority.PERIODIC_QUERY)
            except Exception:  # pylint: disable=broad-exception-caught
                continue
            if all(is_valid_colour_query_response(c, r) for c, r in zip(cmds, responses)):
                return responses
        return None

    def _build_error_results(self) -> list[ControlPollResult]:
        ct = self._current_colour_type
        if ct == ColourType.RGBWAF:
            return dali_type8_rgbwaf.handle_poll_controls_result(None)
        if ct == ColourType.COLOUR_TEMPERATURE:
            return dali_type8_tc.handle_poll_controls_result(None)
        if ct == ColourType.PRIMARY_N:
            return dali_type8_primary_n.handle_poll_controls_result(None)
        if ct == ColourType.XY:
            return dali_type8_xy.handle_poll_controls_result(None)
        return []

    def _build_success_results(self, progress: _Type8ColourReadProgress) -> list[ControlPollResult]:
        ct = progress.colour_type
        if ct == ColourType.RGBWAF:
            colour: object = dali_type8_rgbwaf.RgbwafColourValues(**progress.done_values)
        elif ct == ColourType.COLOUR_TEMPERATURE:
            colour = dali_type8_tc.ColourTemperatureValue(**progress.done_values)
        elif ct == ColourType.PRIMARY_N:
            colour = dali_type8_primary_n.PrimaryNColourValues(**progress.done_values)
        elif ct == ColourType.XY:
            colour = dali_type8_xy.XYColourValues(**progress.done_values)
        else:
            return []
        self._colour_value = colour
        return self._format_colour(ct, colour)

    def _clamp_tc(self, colour: object) -> None:
        if isinstance(colour, dali_type8_tc.ColourTemperatureValue) and colour.tc != MASK_2BYTES:
            colour.tc = min(max(colour.tc, self._limits.tc_min_mirek), self._limits.tc_max_mirek)

    @staticmethod
    def _format_colour(colour_type: ColourType, colour: object) -> list[ControlPollResult]:
        if colour_type == ColourType.RGBWAF:
            return dali_type8_rgbwaf.handle_poll_controls_result(colour)
        if colour_type == ColourType.COLOUR_TEMPERATURE:
            return dali_type8_tc.handle_poll_controls_result(colour)
        if colour_type == ColourType.PRIMARY_N:
            return dali_type8_primary_n.handle_poll_controls_result(colour)
        if colour_type == ColourType.XY:
            return dali_type8_xy.handle_poll_controls_result(colour)
        return []

    def _known_colour_results(self, colour_type: ColourType, colour: object) -> list[ControlPollResult]:
        """Format ``colour`` but drop topics still at their MASK sentinel value.

        A MASK component is one this optimistic colour never set (and no prior read
        filled in); publishing it would emit the sentinel as a real value. The MASK
        representation per topic is whatever a fresh all-MASK colour formats to, so this
        stays type-agnostic.
        """
        masked = {
            res.control_id: res.value
            for res in self._format_colour(colour_type, ColourSettings(colour_type).colour)
        }
        return [
            res for res in self._format_colour(colour_type, colour) if res.value != masked.get(res.control_id)
        ]

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
        res = await query_response(
            driver, QueryColourStatus(short_address), logger, FramePriority.CONFIGURATION
        )
        if getattr(res, "colour_type_xy_active") is True:
            return ColourType.XY
        if getattr(res, "colour_type_colour_temperature_Tc_active") is True:
            return ColourType.COLOUR_TEMPERATURE
        if getattr(res, "colour_type_primary_N_active") is True:
            return ColourType.PRIMARY_N
        return ColourType.RGBWAF
