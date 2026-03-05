from dali.address import GearShort
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
from .wbdali_utils import MASK, WBDALIDriver, check_query_response

SCENES_TOTAL = 16
GROUPS_TOTAL = 16


class MaxLevelParam(NumberGearParam):
    query_command_class = QueryMaxLevel
    set_command_class = SetMaxLevel

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Max level", "Максимальный уровень"), "max_level")
        self.maximum = 254
        self.default = 254
        self.format = "dali-level"
        self.grid_columns = 6
        self.property_order = 16


class MinLevelParam(NumberGearParam):
    query_command_class = QueryMinLevel
    set_command_class = SetMinLevel

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Min level", "Минимальный уровень"), "min_level")
        self.maximum = 254
        self.format = "dali-level"
        self.grid_columns = 6
        self.property_order = 17


class PowerOnLevelParam(NumberGearParam):
    query_command_class = QueryPowerOnLevel
    set_command_class = SetPowerOnLevel

    def __init__(self) -> None:
        super().__init__(
            SettingsParamName("Power on level", "Уровень при включении питания"), "power_on_level"
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
            SettingsParamName("System failure level", "Уровень при сбое"), "system_failure_level"
        )
        self.default = 254
        self.format = "dali-level"
        self.grid_columns = 6
        self.property_order = 30


class FadeTimeFadeRateParam(SettingsParamBase):

    def __init__(self) -> None:
        super().__init__(SettingsParamName("Fade time and fade rate"))
        self._fade_time = None
        self._fade_rate = None

    async def read(self, driver: WBDALIDriver, short_address: int):
        value = await driver.send(QueryFadeTimeFadeRate(GearShort(short_address)))
        check_query_response(value)
        self._fade_time = value.fade_time
        self._fade_rate = value.fade_rate
        return {
            "fade_time": value.fade_time,
            "fade_rate": value.fade_rate,
        }

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        fade_rate_to_set = value.get("fade_rate")
        fade_time_to_set = value.get("fade_time")

        if fade_rate_to_set is None and fade_time_to_set is None:
            return {}
        if self._fade_time == fade_time_to_set and self._fade_rate == fade_rate_to_set:
            return {}

        address = GearShort(short_address)

        commands = []
        if fade_time_to_set is not None:
            commands.append(DTR0(fade_time_to_set))
            commands.append(SetFadeTime(address))
        if fade_rate_to_set is not None:
            commands.append(DTR0(fade_rate_to_set))
            commands.append(SetFadeRate(address))
        commands.append(QueryFadeTimeFadeRate(address))

        last_response = (await driver.send_commands(commands))[-1]
        check_query_response(last_response)
        self._fade_time = last_response.fade_time
        self._fade_rate = last_response.fade_rate
        return {
            "fade_time": self._fade_time,
            "fade_rate": self._fade_rate,
        }


class GroupsParam(SettingsParamBase):
    def __init__(self) -> None:
        super().__init__(SettingsParamName("Groups"))
        self._groups = [False for _ in range(GROUPS_TOTAL)]

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        groups = []
        address = GearShort(short_address)
        commands = [QueryGroupsZeroToSeven(address), QueryGroupsEightToFifteen(address)]
        responses = await driver.send_commands(commands)
        for response in responses:
            check_query_response(response)
            groups.extend([((response.raw_value.as_integer >> i) & 1) == 1 for i in range(8)])
        self._groups = groups
        return {"groups": groups}

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
        groups_to_set = value.get("groups")
        if groups_to_set is None:
            return {}
        if self._groups == groups_to_set:
            return {}

        address = GearShort(short_address)
        commands = []
        for i in range(GROUPS_TOTAL):
            if groups_to_set[i] != self._groups[i]:
                if groups_to_set[i]:
                    commands.append(AddToGroup(address, i))
                else:
                    commands.append(RemoveFromGroup(address, i))
        if not commands:
            return {}
        commands.append(QueryGroupsZeroToSeven(address))
        commands.append(QueryGroupsEightToFifteen(address))
        responses = await driver.send_commands(commands)
        groups = []
        for response in responses[-2:]:
            check_query_response(response)
            groups.extend([((response.raw_value.as_integer >> i) & 1) == 1 for i in range(8)])
        self._groups = groups
        return {"groups": groups}


class ScenesParam(SettingsParamBase):
    def __init__(self) -> None:
        super().__init__(SettingsParamName("Scenes", "Сцены"))
        self._scenes = [MASK for _ in range(SCENES_TOTAL)]

    async def read(self, driver: WBDALIDriver, short_address: int) -> dict:
        address = GearShort(short_address)
        commands = [QuerySceneLevel(address, scene_number) for scene_number in range(SCENES_TOTAL)]
        responses = await driver.send_commands(commands)
        res = []
        for response in responses:
            check_query_response(response)
            res.append(response.raw_value.as_integer)
        self._scenes = res
        return self._scenes_to_json()

    async def write(self, driver: WBDALIDriver, short_address: int, value: dict) -> dict:
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

        address = GearShort(short_address)
        commands = []
        modified_scene_indexes = []
        for i in range(SCENES_TOTAL):
            if self._scenes[i] != values_to_set[i]:
                commands.extend([DTR0(values_to_set[i]), SetScene(address, i), QuerySceneLevel(address, i)])
                modified_scene_indexes.append(i)
        if not commands:
            return {}
        responses = await driver.send_commands(commands)
        for idx, scene_index in enumerate(modified_scene_indexes):
            response = responses[idx * 3 + 2]
            check_query_response(response)
            self._scenes[scene_index] = response.raw_value.as_integer
        return self._scenes_to_json()

    def get_schema(self) -> dict:
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

    def _scenes_to_json(self) -> dict:
        result_scenes = []
        for scene_value in self._scenes:
            if scene_value == MASK:
                result_scenes.append({"enabled": False, "level": 0})
            else:
                result_scenes.append({"enabled": True, "level": scene_value})
        return {"scenes": result_scenes}
