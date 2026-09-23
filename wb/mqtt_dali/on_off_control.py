"""Configurable on/off MQTT control for devices and groups."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import jsonschema
from dali.address import Address
from dali.command import Command
from dali.gear.general import (
    DAPC,
    DTR0,
    GoToLastActiveLevel,
    GoToScene,
    Off,
    SetFadeTime,
)

from .common_dali_device import MqttControlBase, NotifyResult, PropertyStartOrder
from .control_ids import ON_OFF
from .dali_common_parameters import (
    FADE_TIME_ENUM_TITLES,
    SCENES_TOTAL,
    FadeTimeFadeRateParam,
)
from .dali_dimming_curve import DimmingCurveState
from .device_publisher import ControlInfo
from .events import BusEvent, EventSource, LevelChanged
from .settings import SettingsParamBase, SettingsParamName
from .wbdali import WBDALIDriver
from .wbdali_utils import is_broadcast_or_group_address
from .wbmqtt import ControlError, ControlMeta, ControlState, TranslatedTitle

FADE_TIME_CODE_MAX = 15
# Editor-only sentinel: "use the device's own fade time". It never reaches the config
# file — there the same intent is expressed by omitting the ``fade_time`` key.
FADE_TIME_USE_DEVICE = -1

_LAST_ACTIVE_LEVEL_HINT = (
    "Return to last active level is not supported by all devices; if the device does not "
    "implement it, the command has no effect — a device limitation, not a misconfiguration."
)
_LAST_ACTIVE_LEVEL_HINT_RU = (
    "Возврат к последней активной яркости поддерживается не всеми устройствами. Если "
    "устройство этого не умеет, команда не сработает — это ограничение устройства, а не "
    "ошибка настройки."
)
_GROUP_FADE_TIME_HINT = (
    "A group action writes this fade time to every member of the group and leaves it in "
    "force: the members' own fade times are overwritten and not restored."
)
_GROUP_FADE_TIME_HINT_RU = (
    "Групповое действие записывает это время изменения всем участникам группы и оставляет "
    "его действующим: собственные значения участников перезаписываются и не восстанавливаются."
)


class OnActionMode(Enum):
    SCENE = "scene"
    LAST_ACTIVE_LEVEL = "last_active_level"
    LEVEL = "level"
    DAPC = "dapc"


class OffActionMode(Enum):
    OFF = "off"
    DAPC = "dapc"


@dataclass(frozen=True)
class OnAction:
    mode: OnActionMode
    scene: Optional[int] = None
    percent: Optional[int] = None
    value: Optional[int] = None
    # None leaves the device's own fade time untouched (no set, no restore).
    fade_time: Optional[int] = None


@dataclass(frozen=True)
class OffAction:
    mode: OffActionMode
    # None leaves the device's own fade time untouched (no set, no restore).
    fade_time: Optional[int] = None


@dataclass(frozen=True)
class OnOffConfig:
    on_action: OnAction
    off_action: OffAction


def on_off_config_from_json(data: dict) -> OnOffConfig:
    """Requiredness is checked here; field ranges are the JSON schemas' job. Fields of
    a non-selected mode are ignored, so editor deep-merge residue is harmless."""
    if not isinstance(data, dict):
        raise ValueError("on_off must be an object")
    return OnOffConfig(
        on_action=_parse_on_action(_require_object(data, "on_action")),
        off_action=_parse_off_action(_require_object(data, "off_action")),
    )


def on_off_config_to_json(config: OnOffConfig) -> dict:
    return {
        "on_action": _on_action_to_json(config.on_action),
        "off_action": _off_action_to_json(config.off_action),
    }


def gear_params_only(params: dict) -> dict:
    """The parameters that address gear: the on/off block is a service-side setting."""
    return {key: value for key, value in params.items() if key != ON_OFF}


def on_off_config_from_editor_json(data: dict) -> Optional[OnOffConfig]:
    """``enabled: false`` returns ``None``; the rest of the block is deliberately not
    validated. The config file never carries ``enabled`` — a present block means enabled."""
    if not isinstance(data, dict):
        raise ValueError("on_off must be an object")
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("on_off.enabled must be a boolean")
    if not enabled:
        return None
    return on_off_config_from_json(data)


def on_off_config_to_editor_json(config: Optional[OnOffConfig]) -> dict:
    if config is None:
        return {"enabled": False}
    data = on_off_config_to_json(config)
    # The editor fills an absent fade_time with its default, so send that default explicitly.
    if config.on_action.mode is not OnActionMode.SCENE:
        data["on_action"].setdefault("fade_time", FADE_TIME_USE_DEVICE)
    if config.off_action.mode is OffActionMode.DAPC:
        data["off_action"].setdefault("fade_time", FADE_TIME_USE_DEVICE)
    return {"enabled": True, **data}


class OnOffSettingsParam(SettingsParamBase):

    def __init__(self, config: Optional[OnOffConfig] = None) -> None:
        super().__init__(SettingsParamName("On/Off control", "Контрол включения/выключения"))
        self._config = config
        # Kludge: nothing in the SettingsParamBase contract can carry "this particular write
        # needs the controls redrawn", so write() leaves the verdict here -- true right after it
        # and stale at any other moment. Only an appearing or disappearing control needs the
        # rebuild; the strategy of a published one is read from this param live. The fix --
        # write() and has_changes() returning the verdict themselves -- is planned for one of the
        # next updates.
        self.requires_mqtt_controls_refresh = False

    @property
    def config(self) -> Optional[OnOffConfig]:
        """``None`` when no on/off block is set: absence is how the setting is switched off."""
        return self._config

    async def read(
        self, driver: WBDALIDriver, short_address: Address, logger: Optional[logging.Logger] = None
    ) -> dict:
        del driver, short_address, logger
        return {"on_off": on_off_config_to_editor_json(self._config)}

    async def write(
        self,
        driver: WBDALIDriver,
        short_address: Address,
        value: dict,
        logger: Optional[logging.Logger] = None,
    ) -> dict:
        del driver, logger
        if "on_off" not in value:
            return {}
        config = on_off_config_from_editor_json(value["on_off"])
        # The parser checks what is required, the schema the ranges of what is there.
        if config is not None:
            jsonschema.validate(
                instance={"on_off": value["on_off"]},
                schema=on_off_editor_schema(is_broadcast_or_group_address(short_address)),
            )
        if config == self._config:
            return {}
        self.requires_mqtt_controls_refresh = (config is None) != (self._config is None)
        self._config = config
        return {"on_off": on_off_config_to_editor_json(config)}

    def has_changes(self, new_params: dict) -> bool:
        return "on_off" in new_params

    def get_schema(self, group_and_broadcast: bool) -> dict:
        return on_off_editor_schema(group_and_broadcast)


class OnOffControl(MqttControlBase):

    def __init__(
        self,
        param: OnOffSettingsParam,
        dimming_curve_state: DimmingCurveState,
        fade_param: Optional[FadeTimeFadeRateParam] = None,
    ) -> None:
        super().__init__(
            ControlInfo(
                ON_OFF,
                ControlState(
                    ControlMeta("switch", TranslatedTitle("On / Off", "Вкл / выкл")),
                    "0",
                    ControlError.READ,
                ),
            )
        )
        # A published control always has a config: the setting losing it takes the control away.
        self._param = param
        self._dimming_curve_state = dimming_curve_state
        # No fade param means nothing to restore: a group action overwrites every member's fade.
        self._fade_param = fade_param

    def is_writable(self) -> bool:
        return True

    def get_setup_commands(self, short_address: Address, value_to_set: str) -> list[Command]:
        if value_to_set not in ("0", "1"):
            raise ValueError("on_off accepts only 0 or 1")
        # A read error means the shown value is not the gear's: never suppress a write on it.
        if not self.control_info.state.error and value_to_set == self.control_info.state.value:
            return []
        if value_to_set == "1":
            return self._on_commands(short_address)
        return self._off_commands(short_address)

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, LevelChanged):
            return NotifyResult.NOTHING_TO_PUBLISH
        if event.failed:
            self.control_info.state.error = ControlError.READ
            return NotifyResult.PUBLISH_STATE
        if event.raw_level is None:
            return NotifyResult.NOTHING_TO_PUBLISH
        # Applying a predicted value while our own read is failing would clear /meta/error=r.
        if event.source is EventSource.OBSERVED and self.control_info.state.error:
            return NotifyResult.NOTHING_TO_PUBLISH
        self.control_info.state.value = "0" if event.raw_level == 0 else "1"
        self.control_info.state.error = ControlError.NONE
        return NotifyResult.PUBLISH_STATE

    def update_from_percent(self, percent: str) -> None:
        """Track the same ``%`` string ``actual_level`` carries, for a group's seed."""
        try:
            self.control_info.state.value = "0" if float(percent) == 0.0 else "1"
        except ValueError:
            return
        self.control_info.state.error = ControlError.NONE

    # --- Private ---

    def _on_commands(self, short_address: Address) -> list[Command]:
        action = self._param.config.on_action
        if action.mode is OnActionMode.SCENE:
            return [GoToScene(short_address, action.scene)]
        if action.mode is OnActionMode.LAST_ACTIVE_LEVEL:
            command: Command = GoToLastActiveLevel(short_address)
        elif action.mode is OnActionMode.LEVEL:
            command = DAPC(short_address, self._dimming_curve_state.get_raw_value(action.percent))
        else:
            command = DAPC(short_address, action.value)
        return self._with_fade_time(short_address, command, action.fade_time)

    def _off_commands(self, short_address: Address) -> list[Command]:
        action = self._param.config.off_action
        if action.mode is OffActionMode.OFF:
            return [Off(short_address)]
        return self._with_fade_time(short_address, DAPC(short_address, 0), action.fade_time)

    def _with_fade_time(
        self, short_address: Address, command: Command, fade_time: Optional[int]
    ) -> list[Command]:
        if fade_time is None:
            return [command]
        prior = self._fade_param.fade_time if self._fade_param is not None else None
        commands: list[Command] = []
        # An already-matching code skips the write to spare the ballast NVM.
        if prior != fade_time:
            commands += [DTR0(fade_time), SetFadeTime(short_address)]
        commands.append(command)
        # 62386-102 9.5.7: a fade time stored during a running fade does not disturb it.
        if prior is not None and prior != fade_time:
            commands += [DTR0(prior), SetFadeTime(short_address)]
        return commands


def on_off_editor_schema(group_and_broadcast: bool = False) -> dict:
    return {
        "properties": {
            "on_off": {
                "type": "object",
                "title": "On/Off control",
                "format": "dali-on-off",
                "propertyOrder": PropertyStartOrder.ON_OFF.value,
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "title": "Publish",
                        "format": "switch",
                        "propertyOrder": 1,
                    },
                    "on_action": {
                        "type": "object",
                        "title": "When turned on",
                        "propertyOrder": 2,
                        "required": ["mode"],
                        "properties": {
                            "mode": _mode_property(
                                OnActionMode,
                                [
                                    "go to scene",
                                    "restore last active level",
                                    "set brightness",
                                    "direct arc power control",
                                ],
                                OnActionMode.LEVEL,
                                _LAST_ACTIVE_LEVEL_HINT,
                            ),
                            "scene": {
                                "type": "integer",
                                "title": "Scene",
                                "enum": list(range(SCENES_TOTAL)),
                                "propertyOrder": 2,
                                "options": {"grid_columns": 6},
                            },
                            "percent": {
                                "type": "integer",
                                "title": "Level, %",
                                "format": "range",
                                "minimum": 1,
                                "maximum": 100,
                                "propertyOrder": 3,
                                "options": {"grid_columns": 6},
                            },
                            "value": {
                                "type": "integer",
                                "title": "Value",
                                "minimum": 1,
                                "maximum": 254,
                                "propertyOrder": 4,
                                "options": {"grid_columns": 6},
                            },
                            "fade_time": _fade_time_property(5, group_and_broadcast),
                        },
                    },
                    "off_action": {
                        "type": "object",
                        "title": "When turned off",
                        "propertyOrder": 3,
                        "required": ["mode"],
                        "properties": {
                            "mode": _mode_property(
                                OffActionMode,
                                ["turn off immediately", "fade to off"],
                                OffActionMode.OFF,
                            ),
                            "fade_time": _fade_time_property(2, group_and_broadcast),
                        },
                    },
                },
            },
        },
        "translations": {
            "ru": {
                "On/Off control": "Контрол включения/выключения",
                "When turned on": "При включении",
                "When turned off": "При выключении",
                "What to do": "Действие",
                "go to scene": "перейти к сцене",
                "restore last active level": "вернуть последнюю активную яркость",
                "set brightness": "задать яркость",
                "direct arc power control": "прямое управление яркостью",
                "turn off immediately": "выключить сразу",
                "fade to off": "выключить плавно",
                "Publish": "Публиковать",
                "Scene": "Сцена",
                "Level, %": "Яркость, %",
                "Value": "Значение",
                "Fade Time, s": "Время изменения, с",
                "no fade": "мгновенно",
                "use device settings": "использовать настройки устройства",
                _LAST_ACTIVE_LEVEL_HINT: _LAST_ACTIVE_LEVEL_HINT_RU,
                _GROUP_FADE_TIME_HINT: _GROUP_FADE_TIME_HINT_RU,
            },
        },
    }


def _mode_property(
    modes: type[Enum], titles: list[str], default: Enum, description: Optional[str] = None
) -> dict:
    prop = {
        "type": "string",
        "title": "What to do",
        "propertyOrder": 1,
        "default": default.value,
        "enum": [mode.value for mode in modes],
        "options": {"enum_titles": titles},
    }
    if description is not None:
        prop["description"] = description
    return prop


def _fade_time_property(order: int, group_and_broadcast: bool = False) -> dict:
    prop = {
        "type": "integer",
        "title": "Fade Time, s",
        "propertyOrder": order,
        "default": FADE_TIME_USE_DEVICE,
        "enum": [FADE_TIME_USE_DEVICE, *range(FADE_TIME_CODE_MAX + 1)],
        "options": {
            "enum_titles": ["use device settings", *FADE_TIME_ENUM_TITLES],
            "grid_columns": 6,
        },
    }
    if group_and_broadcast:
        prop["description"] = _GROUP_FADE_TIME_HINT
    return prop


def _require_object(data: dict, key: str) -> dict:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"on_off.{key} must be an object")
    return value


def _int_field(data: dict, key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"on_off action requires an integer '{key}' field")
    return value


def _optional_fade_time(data: dict) -> Optional[int]:
    if "fade_time" not in data:
        return None
    value = _int_field(data, "fade_time")
    if value == FADE_TIME_USE_DEVICE:
        return None
    return value


def _parse_on_action(data: dict) -> OnAction:
    try:
        mode = OnActionMode(data.get("mode"))
    except ValueError as exc:
        raise ValueError(f"Unknown on_action mode: {data.get('mode')!r}") from exc
    if mode is OnActionMode.SCENE:
        return OnAction(mode, scene=_int_field(data, "scene"))
    if mode is OnActionMode.LEVEL:
        return OnAction(mode, percent=_int_field(data, "percent"), fade_time=_optional_fade_time(data))
    if mode is OnActionMode.DAPC:
        return OnAction(mode, value=_int_field(data, "value"), fade_time=_optional_fade_time(data))
    return OnAction(mode, fade_time=_optional_fade_time(data))


def _parse_off_action(data: dict) -> OffAction:
    try:
        mode = OffActionMode(data.get("mode"))
    except ValueError as exc:
        raise ValueError(f"Unknown off_action mode: {data.get('mode')!r}") from exc
    if mode is OffActionMode.DAPC:
        return OffAction(mode, fade_time=_optional_fade_time(data))
    return OffAction(mode)


def _on_action_to_json(action: OnAction) -> dict:
    data: dict = {"mode": action.mode.value}
    if action.scene is not None:
        data["scene"] = action.scene
    if action.percent is not None:
        data["percent"] = action.percent
    if action.value is not None:
        data["value"] = action.value
    if action.fade_time is not None:
        data["fade_time"] = action.fade_time
    return data


def _off_action_to_json(action: OffAction) -> dict:
    data: dict = {"mode": action.mode.value}
    if action.fade_time is not None:
        data["fade_time"] = action.fade_time
    return data
