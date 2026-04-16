import logging
from typing import Optional

from dali.address import Address
from dali.gear.general import (
    DTR0,
    AddToGroup,
    QueryFadeTimeFadeRate,
    QueryGroupsEightToFifteen,
    QueryGroupsZeroToSeven,
    QueryMaxLevel,
    QueryMinLevel,
    QueryPowerOnLevel,
    QuerySceneLevel,
    QuerySystemFailureLevel,
    RemoveFromGroup,
    SetFadeRate,
    SetFadeTime,
    SetMaxLevel,
    SetMinLevel,
    SetPowerOnLevel,
    SetScene,
    SetSystemFailureLevel,
)

from .dali_parameters import NumberGearParam
from .settings import SettingsParamBase, SettingsParamName
from .wbdali_utils import (
    MASK,
    WBDALIDriver,
    is_broadcast_or_group_address,
    query_response,
    query_responses,
    query_responses_retry_from_first_failed,
    query_responses_retry_only_failed,
    send_commands_with_retry,
)

# pylint: disable=duplicate-code


SCENES_TOTAL = 16
GROUPS_TOTAL = 16


class MaxLevelParam(NumberGearParam):
    query_command_class = QueryMaxLevel
    set_command_class = SetMaxLevel

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Max level", "Максимальная яркость"), "max_level")
        self.maximum = 254
        self.default = 254
        self.format = "dali-level"
        self.grid_columns = 6
        self.property_order = 16


class MinLevelParam(NumberGearParam):
    query_command_class = QueryMinLevel
    set_command_class = SetMinLevel

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Min level", "Минимальная яркость"), "min_level")
        self.maximum = 254
        self.format = "dali-level"
        self.grid_columns = 6
        self.property_order = 17


class PowerOnLevelParam(NumberGearParam):
    query_command_class = QueryPowerOnLevel
    set_command_class = SetPowerOnLevel

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Power on level", "Яркость при включении питания"), "power_on_level"
        )
        self.default = 254
        self.format = "dali-level"
        self.grid_columns = 6
        self.property_order = 21


class SystemFailureLevelParam(NumberGearParam):
    query_command_class = QuerySystemFailureLevel
    set_command_class = SetSystemFailureLevel

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("System failure level", "Яркость при сбое"), "system_failure_level"
        )
        self.default = 254
        self.format = "dali-level"
        self.grid_columns = 6
        self.property_order = 30


class FadeTimeFadeRateParam(SettingsParamBase):

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Fade time and fade rate", "Время и скорость затухания"))
        self._fade_time = None
        self._fade_rate = None

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        value = await query_response(driver, QueryFadeTimeFadeRate(short_address), logger)
        self._fade_time = value.fade_time
        self._fade_rate = value.fade_rate
        return {
            "fade_time": value.fade_time,
            "fade_rate": value.fade_rate,
        }

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        fade_rate_to_set = value.get("fade_rate")
        fade_time_to_set = value.get("fade_time")

        if fade_rate_to_set is None and fade_time_to_set is None:
            return {}

        is_for_single_device = not is_broadcast_or_group_address(short_address)
        if (
            is_for_single_device
            and self._fade_time == fade_time_to_set
            and self._fade_rate == fade_rate_to_set
        ):
            return {}

        commands = []
        if fade_time_to_set is not None:
            commands.append(DTR0(fade_time_to_set))
            commands.append(SetFadeTime(short_address))
        if fade_rate_to_set is not None:
            commands.append(DTR0(fade_rate_to_set))
            commands.append(SetFadeRate(short_address))
        if is_for_single_device:
            commands.append(QueryFadeTimeFadeRate(short_address))
        responses = await query_responses(driver, commands, logger)

        if not is_for_single_device:
            return {}

        last_response = responses[-1]
        self._fade_time = last_response.fade_time
        self._fade_rate = last_response.fade_rate
        return {
            "fade_time": self._fade_time,
            "fade_rate": self._fade_rate,
        }

    def has_changes(self, new_params: dict) -> bool:
        return "fade_time" in new_params or "fade_rate" in new_params

    def get_schema(self, group_and_broadcast: bool) -> dict:
        return {
            "properties": {
                "fade_time": {
                    "type": "number",
                    "title": "Fade Time, s",
                    "propertyOrder": 19,
                    "enum": list(range(16)),
                    "default": 0,
                    "options": {
                        "enum_titles": [
                            "no fade",
                            "0.7",
                            "1.0",
                            "1.4",
                            "2.0",
                            "2.8",
                            "4.0",
                            "5.7",
                            "8.0",
                            "11.3",
                            "16.0",
                            "22.6",
                            "32.0",
                            "45.3",
                            "64.0",
                            "90.5",
                        ],
                        "grid_columns": 6,
                        "wb": {
                            "show_editor": True,
                        },
                    },
                },
                "fade_rate": {
                    "type": "number",
                    "title": "Fade Rate, steps/s",
                    "propertyOrder": 20,
                    "enum": list(range(1, 16)),
                    "default": 1,
                    "options": {
                        "enum_titles": [
                            "358",
                            "253",
                            "179",
                            "127",
                            "89.4",
                            "63.3",
                            "44.7",
                            "31.6",
                            "22.4",
                            "15.8",
                            "11.2",
                            "7.9",
                            "5.6",
                            "4.0",
                            "2.8",
                        ],
                        "grid_columns": 6,
                        "wb": {
                            "show_editor": True,
                        },
                    },
                },
            },
            "translations": {
                "ru": {
                    "Fade Time, s": "Время затухания, с",
                    "Fade Rate, steps/s": "Скорость затухания, шаг/с",
                    "no fade": "без затухания",
                },
            },
        }


class GroupsParam(SettingsParamBase):
    def __init__(self) -> None:
        super().__init__(SettingsParamName("Groups", "Группы"))
        self._groups = [False for _ in range(GROUPS_TOTAL)]
        self._group_indexes = set()

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        groups = []
        commands = [QueryGroupsZeroToSeven(short_address), QueryGroupsEightToFifteen(short_address)]
        responses = await query_responses_retry_only_failed(driver, commands, logger)
        for response in responses:
            groups.extend([((response.raw_value.as_integer >> i) & 1) == 1 for i in range(8)])
        self._groups = groups
        self._group_indexes = {i for i, in_group in enumerate(groups) if in_group}
        return {"groups": groups}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        groups_to_set = value.get("groups")
        if groups_to_set is None:
            return {}
        if self._groups == groups_to_set:
            return {}

        commands = []
        for i in range(GROUPS_TOTAL):
            if groups_to_set[i] != self._groups[i]:
                if groups_to_set[i]:
                    commands.append(AddToGroup(short_address, i))
                else:
                    commands.append(RemoveFromGroup(short_address, i))
        if not commands:
            return {}
        commands.append(QueryGroupsZeroToSeven(short_address))
        commands.append(QueryGroupsEightToFifteen(short_address))
        responses = await query_responses_retry_from_first_failed(driver, commands, logger=logger)
        groups = []
        for response in responses[-2:]:
            groups.extend([((response.raw_value.as_integer >> i) & 1) == 1 for i in range(8)])
        self._groups = groups
        self._group_indexes = {i for i, in_group in enumerate(groups) if in_group}
        return {"groups": groups}

    def has_changes(self, new_params: dict) -> bool:
        return "groups" in new_params

    @property
    def groups(self) -> set[int]:
        return self._group_indexes


class ScenesParam(SettingsParamBase):
    def __init__(self) -> None:
        super().__init__(SettingsParamName("Scenes", "Сцены"))
        self._scenes = [MASK for _ in range(SCENES_TOTAL)]

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        commands = [QuerySceneLevel(short_address, scene_number) for scene_number in range(SCENES_TOTAL)]
        responses = await query_responses_retry_only_failed(driver, commands, logger)
        res = []
        for response in responses:
            res.append(response.raw_value.as_integer)
        self._scenes = res
        return self._scenes_to_json()

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        scenes = value.get("scenes")
        if scenes is None:
            return {}
        values_to_set = [MASK for _ in range(SCENES_TOTAL)]
        for i in range(SCENES_TOTAL):
            scene = scenes[i]
            if scene.get("enabled", False) is False:
                values_to_set[i] = MASK
            else:
                values_to_set[i] = scene.get("level", MASK)

        commands = []
        modified_scene_indexes = []
        for i in range(SCENES_TOTAL):
            if self._scenes[i] != values_to_set[i]:
                commands.extend(
                    [DTR0(values_to_set[i]), SetScene(short_address, i), QuerySceneLevel(short_address, i)]
                )
                modified_scene_indexes.append(i)
        if not commands:
            return {}
        responses = await query_responses_retry_from_first_failed(driver, commands, 3, logger)
        for idx, scene_index in enumerate(modified_scene_indexes):
            response = responses[idx * 3 + 2]
            self._scenes[scene_index] = response.raw_value.as_integer
        return self._scenes_to_json()

    def get_schema(self, group_and_broadcast: bool) -> dict:
        return {
            "properties": {
                "scenes": {
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
                                "format": "switch",
                                "propertyOrder": 1,
                                "options": {
                                    "compact": True,
                                    "grid_columns": 2,
                                },
                            },
                            "level": {
                                "type": "integer",
                                "title": "Light level",
                                "format": "dali-level",
                                "minimum": 0,
                                "maximum": 254,
                                "propertyOrder": 2,
                                "options": {
                                    "grid_columns": 4,
                                },
                            },
                        },
                        "required": ["enabled", "level"],
                    },
                    "propertyOrder": 807,
                },
            },
            "translations": {
                "ru": {
                    self.name.en: self.name.ru,
                    "Part of the scene": "Часть сцены",
                    "Light level": "Яркость",
                },
            },
        }

    def has_changes(self, new_params: dict) -> bool:
        return "scenes" in new_params

    def _scenes_to_json(self) -> dict:
        result_scenes = []
        for scene_value in self._scenes:
            if scene_value == MASK:
                result_scenes.append({"enabled": False, "level": 0})
            else:
                result_scenes.append({"enabled": True, "level": scene_value})
        return {"scenes": result_scenes}


class GroupScenesSettings(SettingsParamBase):
    def __init__(self) -> None:
        super().__init__(SettingsParamName("Scenes", "Сцены"))
        self.property_name = "scenes"

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        scenes = value.get(self.property_name)
        if scenes is None:
            return {}
        if scenes.get("enabled", False) is False:
            value_to_set = MASK
        else:
            value_to_set = scenes.get("level", MASK)
        index = scenes.get("index")
        if index is None:
            raise ValueError("Scene index is required")
        commands = [DTR0(value_to_set), SetScene(short_address, index)]
        await send_commands_with_retry(driver, commands, logger)
        return {}

    def has_changes(self, new_params: dict) -> bool:
        return self.property_name in new_params

    def get_schema(self, group_and_broadcast: bool) -> dict:
        return {
            "properties": {
                self.property_name: {
                    "type": "object",
                    "title": self.name.en,
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
                        "level": {
                            "type": "integer",
                            "title": "Light level",
                            "format": "dali-level",
                            "minimum": 0,
                            "maximum": 254,
                            "propertyOrder": 2,
                            "options": {
                                "grid_columns": 4,
                            },
                        },
                    },
                    "required": ["index", "enabled", "level"],
                    "options": {
                        "wb": {
                            "show_editor": True,
                        },
                    },
                },
            },
            "translations": {
                "ru": {
                    "Part of the scene": "Часть сцены",
                    "Scene number": "Номер сцены",
                    "Light level": "Яркость",
                    self.name.en: self.name.ru,
                },
            },
        }
