"""Configurable on/off MQTT control for devices and groups."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

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

from .common_dali_device import MqttControlBase, PropertyStartOrder
from .control_ids import ON_OFF
from .dali_common_parameters import (
    FADE_TIME_ENUM_TITLES,
    SCENES_TOTAL,
    FadeTimeFadeRateParam,
)
from .dali_dimming_curve import DimmingCurveState
from .device_publisher import ControlInfo
from .settings import SettingsParamBase, SettingsParamName
from .wbdali import WBDALIDriver
from .wbmqtt import ControlMeta, TranslatedTitle

FADE_TIME_CODE_MAX = 15


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
    return {"enabled": True, **on_off_config_to_json(config)}


class OnOffControl(MqttControlBase):

    def __init__(
        self,
        config: OnOffConfig,
        dimming_curve_state: DimmingCurveState,
    ) -> None:
        super().__init__(
            ControlInfo(ON_OFF, ControlMeta("switch", TranslatedTitle("On / Off", "Вкл / выкл")), "0")
        )
        self._config = config
        self._dimming_curve_state = dimming_curve_state
        # The default "0" is a placeholder, not an observed state: suppress same-value
        # writes only once a real level (a write or a readback) has established the state.
        self._state_known = False

    def is_writable(self) -> bool:
        return True

    def get_setup_commands(self, short_address: Address, value_to_set: str) -> list[Command]:
        if value_to_set not in ("0", "1"):
            raise ValueError("on_off accepts only 0 or 1")
        if self._state_known and value_to_set == self.control_info.value:
            return []
        # Accepting the write establishes the state, so a same-value re-write during the
        # optimistic window (before the confirming poll lands) is suppressed too.
        self._state_known = True
        if value_to_set == "1":
            return self._on_commands(short_address)
        return self._off_commands(short_address)

    def update_from_percent(self, percent: str) -> Optional[str]:
        """Track the same ``%`` string that ``actual_level`` publishes."""
        try:
            state = "0" if float(percent) == 0.0 else "1"
        except ValueError:
            return None
        self._state_known = True
        self.control_info.value = state
        return state

    def set_config(self, config: OnOffConfig) -> None:
        """Swap the on/off strategy without disturbing the known switch state: the MQTT
        control (a ``switch``) and its value are unchanged, only future commands differ."""
        self._config = config

    # --- Hooks for subclasses ---

    def _prior_fade_time(self) -> Optional[int]:
        raise NotImplementedError

    # --- Private ---

    def _on_commands(self, short_address: Address) -> list[Command]:
        action = self._config.on_action
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
        action = self._config.off_action
        if action.mode is OffActionMode.OFF:
            return [Off(short_address)]
        return self._with_fade_time(short_address, DAPC(short_address, 0), action.fade_time)

    def _with_fade_time(
        self, short_address: Address, command: Command, fade_time: Optional[int]
    ) -> list[Command]:
        if fade_time is None:
            return [command]
        prior = self._prior_fade_time()
        commands: list[Command] = []
        # An already-matching code skips the write to spare the ballast NVM.
        if prior != fade_time:
            commands += [DTR0(fade_time), SetFadeTime(short_address)]
        commands.append(command)
        if prior is not None and prior != fade_time:
            commands += [DTR0(prior), SetFadeTime(short_address)]
        return commands


class DeviceOnOffControl(OnOffControl):

    def __init__(
        self,
        config: OnOffConfig,
        dimming_curve_state: DimmingCurveState,
        fade_param: FadeTimeFadeRateParam,
    ) -> None:
        super().__init__(config, dimming_curve_state)
        self._fade_param = fade_param

    # --- Hooks for subclasses ---

    def _prior_fade_time(self) -> Optional[int]:
        return self._fade_param.fade_time


class OnOffSettingsParam(SettingsParamBase):

    requires_mqtt_controls_refresh = True

    def __init__(self, config: Optional[OnOffConfig] = None) -> None:
        super().__init__(SettingsParamName("On/off control", "Управление вкл/выкл"))
        self._config = config

    @property
    def config(self) -> Optional[OnOffConfig]:
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
        del driver, short_address, logger
        if "on_off" not in value:
            return {}
        config = on_off_config_from_editor_json(value["on_off"])
        if config == self._config:
            return {}
        self._config = config
        return {"on_off": on_off_config_to_editor_json(config)}

    def has_changes(self, new_params: dict) -> bool:
        return "on_off" in new_params

    def get_schema(self, group_and_broadcast: bool) -> dict:
        del group_and_broadcast
        return on_off_editor_schema()


def on_off_editor_schema() -> dict:
    return {
        "properties": {
            "on_off": {
                "type": "object",
                "title": "On/off control",
                "format": "dali-on-off",
                "propertyOrder": PropertyStartOrder.ON_OFF.value,
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "title": "Enabled",
                        "propertyOrder": 1,
                    },
                    "on_action": {
                        "type": "object",
                        "title": "On action",
                        "propertyOrder": 2,
                        "properties": {
                            "mode": _mode_property(
                                OnActionMode, ["Scene", "Last active level", "Level", "DAPC"]
                            ),
                            "scene": {
                                "type": "integer",
                                "title": "Scene",
                                "minimum": 0,
                                "maximum": SCENES_TOTAL - 1,
                                "propertyOrder": 2,
                            },
                            "percent": {
                                "type": "integer",
                                "title": "Level, %",
                                "minimum": 1,
                                "maximum": 100,
                                "propertyOrder": 3,
                            },
                            "value": {
                                "type": "integer",
                                "title": "Value",
                                "minimum": 1,
                                "maximum": 254,
                                "propertyOrder": 4,
                            },
                            "fade_time": _fade_time_property(5),
                        },
                    },
                    "off_action": {
                        "type": "object",
                        "title": "Off action",
                        "propertyOrder": 3,
                        "properties": {
                            "mode": _mode_property(OffActionMode, ["Off", "DAPC"]),
                            "fade_time": _fade_time_property(2),
                        },
                    },
                },
            },
        },
        "translations": {
            "ru": {
                "On/off control": "Управление вкл/выкл",
                "Enabled": "Включено",
                "On action": "Действие при включении",
                "Off action": "Действие при выключении",
                "Mode": "Режим",
                "Scene": "Сцена",
                "Last active level": "Последняя активная яркость",
                "Level": "Яркость",
                "Level, %": "Яркость, %",
                "Value": "Значение",
                "Fade Time, s": "Время изменения, с",
                "no fade": "мгновенно",
                "Off": "Выкл",
            },
        },
    }


def _mode_property(modes: type[Enum], titles: list[str]) -> dict:
    return {
        "type": "string",
        "title": "Mode",
        "propertyOrder": 1,
        "enum": [mode.value for mode in modes],
        "options": {"enum_titles": titles},
    }


def _fade_time_property(order: int) -> dict:
    return {
        "type": "integer",
        "title": "Fade Time, s",
        "propertyOrder": order,
        "enum": list(range(FADE_TIME_CODE_MAX + 1)),
        "options": {"enum_titles": list(FADE_TIME_ENUM_TITLES)},
    }


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
    return _int_field(data, "fade_time")


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
