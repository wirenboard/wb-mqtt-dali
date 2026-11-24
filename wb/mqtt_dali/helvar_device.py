import asyncio
import logging
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, NamedTuple, Optional

import jsonschema
from dali.address import GearShort
from dali.command import Response
from dali.gear.general import Initialise, QueryControlGearPresent, Terminate

from .commissioning import MAX_SHORT_ADDRESS
from .common_dali_device import (
    PERIODIC_STATUS_POLL_INTERVAL,
    ApplyResult,
    DaliDeviceAddress,
    InitializationResult,
    MqttControlBase,
    NotifyResult,
    PropertyStartOrder,
    SingleQueryControl,
)
from .dali_common_parameters import FADE_TIME_ENUM_TITLES, GROUPS_TOTAL, SCENES_TOTAL
from .dali_device import DaliDevice
from .device_publisher import ControlInfo
from .events import BusEvent, GearPresentRead
from .gtin_db import DaliDatabase
from .helvar import e2
from .helvar.digidim import QueryMajorVersion, QueryMinorVersion
from .helvar.e2 import DigidimE2, DigidimImage
from .on_off_control import FADE_TIME_CODE_MAX, FADE_TIME_USE_DEVICE, OnOffConfig
from .wbdali import FramePriority, WBDALIDriver
from .wbdali_utils import MASK, query_responses_retry_only_failed, send_with_retry
from .wbmqtt import ALARM_CONTROL_TYPE, ControlMeta, ControlState, TranslatedTitle

HELVAR_BRAND_NAME = "Helvar"
HELVAR_GTIN_PREFIXES = ("5030797", "6418488")
HELVAR_311_GTIN = 5030797003118
HELVAR_321_GTIN = 5030797003217

# Supported models; other Helvar GTINs are named by the GTIN and get no settings.
HELVAR_MODEL_BY_GTIN: dict[int, str] = {
    HELVAR_311_GTIN: "Ceiling PIR Detector 311",
    HELVAR_321_GTIN: "MultiSensor 321",
}

# The sensor blinks its LED while initialised.
IDENTIFY_INITIALISED_TIME_S = 5.0

_UNSUPPORTED_MODEL_NOTE = TranslatedTitle(
    "Helvar Digidim device, this model is not supported",
    "Устройство Helvar Digidim, работа с этой моделью не поддерживается",
)
_SENSOR_NOTE = TranslatedTitle(
    "The sensor sends no events and does not obey DALI commands: it controls other devices itself "
    "by the state of its sensors, as set below",
    "Датчик не отправляет событий и не управляется командами DALI: он сам управляет другими "
    "устройствами по состоянию своих датчиков, как задано ниже",
)
_PROPERTY_ORDER = PropertyStartOrder.SPECIFIC.value
_STATUS_OK_TITLE = TranslatedTitle("Ok", "Норма")
_STATUS_NOT_RESPONDING_TITLE = TranslatedTitle("Not responding", "Не отвечает")


class EventTarget(Enum):
    ADDRESS = "address"
    GROUP = "group"
    BROADCAST = "broadcast"


class EventCommand(Enum):
    """In the order of the form; members named as in `e2.FixedCommand` map to it."""

    NOT_USED = "not_used"
    GO_TO_SCENE = "scene"
    OFF = "off"
    RECALL_MAX = "recall_max"
    RECALL_MIN = "recall_min"
    UP = "up"
    DOWN = "down"
    STEP_UP = "step_up"
    STEP_DOWN = "step_down"
    STEP_DOWN_AND_OFF = "step_down_and_off"
    ON_AND_STEP_UP = "on_and_step_up"
    DIRECT_LEVEL = "direct_level"
    OFF_STORE_LAST_LEVEL = "off_store_last_level"
    MIN_STORE_LAST_LEVEL = "min_store_last_level"
    SCENE_RECALL_STORE = "scene_recall_store"
    SCENE_TOGGLE = "scene_toggle"
    TOUCH_LAST_LEVEL = "touch_last_level"
    TOUCH_MAX_LEVEL = "touch_max_level"
    TOUCH_SCENE_0 = "touch_scene_0"
    TOUCH_SCENE_1 = "touch_scene_1"
    LAST_LEVEL_OFF = "last_level_off"
    MAX_LEVEL_OFF = "max_level_off"
    UP_STEP_UP = "up_step_up"
    UP_STEP_UP_AND_ON = "up_step_up_and_on"
    DOWN_STEP_DOWN = "down_step_down"
    DOWN_STEP_DOWN_AND_OFF = "down_step_down_and_off"
    MAX_LEVEL_UP = "max_level_up"
    MAX_LEVEL_DOWN = "max_level_down"
    LAST_LEVEL_UP = "last_level_up"
    SCENE_0_UP = "scene_0_up"
    SCENE_1_UP = "scene_1_up"
    OFF_UP = "off_up"
    OFF_DOWN = "off_down"
    COLOUR_TEMPERATURE_COOLER = "colour_temperature_cooler"
    COLOUR_TEMPERATURE_WARMER = "colour_temperature_warmer"
    DISABLE_PIR_ON = "disable_pir_on"
    ENABLE_PIR_ON = "enable_pir_on"
    DISABLE_PIR_OFF = "disable_pir_off"
    ENABLE_PIR_OFF = "enable_pir_off"
    DISABLE_MANUAL_CONTROLS = "disable_manual_controls"
    ENABLE_MANUAL_CONTROLS = "enable_manual_controls"
    DISABLE_INFRA_RED = "disable_infra_red"
    ENABLE_INFRA_RED = "enable_infra_red"
    DISABLE_PANEL = "disable_panel"
    ENABLE_PANEL = "enable_panel"
    DISABLE_PROGRAMMING = "disable_programming"
    # Offered only in the Constant light output and where it is read.
    RAMP = "ramp"
    # A slot E2 does not describe; offered only where it is read, and kept as it is.
    UNRECOGNISED = "unrecognised"


_TARGET_BY_CLASS = {
    e2.ShortAddress: EventTarget.ADDRESS,
    e2.Group: EventTarget.GROUP,
    e2.Broadcast: EventTarget.BROADCAST,
}
_FADED_ACTION_CLASS_BY_COMMAND = {
    EventCommand.OFF: e2.Off,
    EventCommand.RECALL_MAX: e2.RecallMax,
    EventCommand.RECALL_MIN: e2.RecallMin,
    EventCommand.UP: e2.Up,
    EventCommand.DOWN: e2.Down,
    EventCommand.STEP_UP: e2.StepUp,
    EventCommand.STEP_DOWN: e2.StepDown,
    EventCommand.STEP_DOWN_AND_OFF: e2.StepDownAndOff,
    EventCommand.ON_AND_STEP_UP: e2.OnAndStepUp,
}
_SCENE_ACTION_CLASS_BY_COMMAND = {
    EventCommand.GO_TO_SCENE: e2.GoToScene,
    EventCommand.SCENE_RECALL_STORE: e2.SceneRecallStore,
    EventCommand.SCENE_TOGGLE: e2.SceneToggle,
}
_UNFADED_ACTION_CLASS_BY_COMMAND = {
    EventCommand.NOT_USED: e2.SendNothing,
    EventCommand.RAMP: e2.Ramp,
}
_COMMAND_BY_ACTION_CLASS = {
    action_class: command
    for action_classes in (
        _FADED_ACTION_CLASS_BY_COMMAND,
        _SCENE_ACTION_CLASS_BY_COMMAND,
        _UNFADED_ACTION_CLASS_BY_COMMAND,
    )
    for command, action_class in action_classes.items()
}
_COMMAND_BY_ACTION_CLASS[e2.DirectLevel] = EventCommand.DIRECT_LEVEL

_LEVEL_TITLE = TranslatedTitle("Direct arc power control value", "Значение прямого управления яркостью")
_FADE_TIME_NOT_SET_TITLE = TranslatedTitle("do not change", "не менять")
_DURATION_STEP_HINT = TranslatedTitle(
    f"Step {e2.DURATION_STEP_S} s, a value in between is rounded to the nearest",
    f"Шаг {e2.DURATION_STEP_S} с, промежуточное значение округляется до ближайшего",
)
_FADE_TIME_HINT = TranslatedTitle(
    "The sensor overwrites the fade time in the settings of the event's targets with this value, "
    "and it stays there",
    "Датчик перепишет этим значением время изменения яркости в настройках адресатов события, "
    "и оно там останется",
)
_TARGET_TITLES = {
    EventTarget.ADDRESS: TranslatedTitle("short address", "короткий адрес"),
    EventTarget.GROUP: TranslatedTitle("group", "группа"),
    EventTarget.BROADCAST: TranslatedTitle("broadcast", "все устройства"),
}
_COMMAND_TITLES = {
    EventCommand.GO_TO_SCENE: TranslatedTitle("go to scene", "перейти к сцене"),
    EventCommand.OFF: TranslatedTitle("off", "выключить"),
    EventCommand.RECALL_MAX: TranslatedTitle("recall max level", "максимальная яркость"),
    EventCommand.RECALL_MIN: TranslatedTitle("recall min level", "минимальная яркость"),
    EventCommand.UP: TranslatedTitle("up", "плавно ярче"),
    EventCommand.DOWN: TranslatedTitle("down", "плавно темнее"),
    EventCommand.STEP_UP: TranslatedTitle("step up", "на шаг ярче"),
    EventCommand.STEP_DOWN: TranslatedTitle("step down", "на шаг темнее"),
    EventCommand.STEP_DOWN_AND_OFF: TranslatedTitle(
        "step down and off", "на шаг темнее, с минимума выключить"
    ),
    EventCommand.ON_AND_STEP_UP: TranslatedTitle("on and step up", "включить или на шаг ярче"),
    EventCommand.DIRECT_LEVEL: TranslatedTitle("direct arc power control", "прямое управление яркостью"),
    EventCommand.OFF_STORE_LAST_LEVEL: TranslatedTitle(
        "off, store last level", "запомнить яркость и выключить"
    ),
    EventCommand.MIN_STORE_LAST_LEVEL: TranslatedTitle(
        "min level, store last level", "запомнить яркость и перейти на минимальную"
    ),
    EventCommand.SCENE_RECALL_STORE: TranslatedTitle(
        "scene, hold to store the level in it", "сцена, удержание — сохранить в неё яркость"
    ),
    EventCommand.SCENE_TOGGLE: TranslatedTitle("scene or off in turn", "сцена или выключить по очереди"),
    EventCommand.TOUCH_LAST_LEVEL: TranslatedTitle(
        "touch: last level or off, hold to fade",
        "касание: последняя яркость или выключить, удержание — плавно",
    ),
    EventCommand.TOUCH_MAX_LEVEL: TranslatedTitle(
        "touch: max level or off, hold to fade",
        "касание: максимальная яркость или выключить, удержание — плавно",
    ),
    EventCommand.TOUCH_SCENE_0: TranslatedTitle(
        "touch: scene 0 or off, hold to fade", "касание: сцена 0 или выключить, удержание — плавно"
    ),
    EventCommand.TOUCH_SCENE_1: TranslatedTitle(
        "touch: scene 1 or off, hold to fade", "касание: сцена 1 или выключить, удержание — плавно"
    ),
    EventCommand.LAST_LEVEL_OFF: TranslatedTitle(
        "last level or off in turn", "последняя яркость или выключить по очереди"
    ),
    EventCommand.MAX_LEVEL_OFF: TranslatedTitle(
        "max level or off in turn", "максимальная яркость или выключить по очереди"
    ),
    EventCommand.UP_STEP_UP: TranslatedTitle(
        "step up, hold to fade up", "на шаг ярче, удержание — плавно ярче"
    ),
    EventCommand.UP_STEP_UP_AND_ON: TranslatedTitle(
        "on and step up, hold to fade up", "включить или на шаг ярче, удержание — плавно ярче"
    ),
    EventCommand.DOWN_STEP_DOWN: TranslatedTitle(
        "step down, hold to fade down", "на шаг темнее, удержание — плавно темнее"
    ),
    EventCommand.DOWN_STEP_DOWN_AND_OFF: TranslatedTitle(
        "step down and off, hold to fade down",
        "на шаг темнее, с минимума выключить, удержание — плавно темнее",
    ),
    EventCommand.MAX_LEVEL_UP: TranslatedTitle(
        "max level, hold to fade up", "максимальная яркость, удержание — плавно ярче"
    ),
    EventCommand.MAX_LEVEL_DOWN: TranslatedTitle(
        "max level, hold to fade down", "максимальная яркость, удержание — плавно темнее"
    ),
    EventCommand.LAST_LEVEL_UP: TranslatedTitle(
        "last level, hold to fade up", "последняя яркость, удержание — плавно ярче"
    ),
    EventCommand.SCENE_0_UP: TranslatedTitle("scene 0, hold to fade up", "сцена 0, удержание — плавно ярче"),
    EventCommand.SCENE_1_UP: TranslatedTitle("scene 1, hold to fade up", "сцена 1, удержание — плавно ярче"),
    EventCommand.OFF_UP: TranslatedTitle("off, hold to fade up", "выключить, удержание — плавно ярче"),
    EventCommand.OFF_DOWN: TranslatedTitle("off, hold to fade down", "выключить, удержание — плавно темнее"),
    EventCommand.COLOUR_TEMPERATURE_COOLER: TranslatedTitle(
        "colour temperature step cooler", "цветовая температура на шаг холоднее"
    ),
    EventCommand.COLOUR_TEMPERATURE_WARMER: TranslatedTitle(
        "colour temperature step warmer", "цветовая температура на шаг теплее"
    ),
    EventCommand.DISABLE_PIR_ON: TranslatedTitle(
        "disable switching on by sensors", "запретить датчикам включать свет"
    ),
    EventCommand.ENABLE_PIR_ON: TranslatedTitle(
        "enable switching on by sensors", "разрешить датчикам включать свет"
    ),
    EventCommand.DISABLE_PIR_OFF: TranslatedTitle(
        "disable switching off by sensors", "запретить датчикам выключать свет"
    ),
    EventCommand.ENABLE_PIR_OFF: TranslatedTitle(
        "enable switching off by sensors", "разрешить датчикам выключать свет"
    ),
    EventCommand.DISABLE_MANUAL_CONTROLS: TranslatedTitle(
        "disable manual controls", "запретить ручное управление"
    ),
    EventCommand.ENABLE_MANUAL_CONTROLS: TranslatedTitle(
        "enable manual controls", "разрешить ручное управление"
    ),
    EventCommand.DISABLE_INFRA_RED: TranslatedTitle(
        "disable infrared until power cycle", "отключить ИК-приёмник до сброса питания"
    ),
    EventCommand.ENABLE_INFRA_RED: TranslatedTitle("enable infrared", "включить ИК-приёмник"),
    EventCommand.DISABLE_PANEL: TranslatedTitle("disable panel", "заблокировать панель"),
    EventCommand.ENABLE_PANEL: TranslatedTitle("enable panel", "разблокировать панель"),
    EventCommand.DISABLE_PROGRAMMING: TranslatedTitle(
        "disable programming (temporary)", "временно запретить настройку"
    ),
    EventCommand.RAMP: TranslatedTitle(
        "adjust level to the light level", "подстройка яркости по освещённости"
    ),
    EventCommand.NOT_USED: TranslatedTitle("send nothing", "не отправлять"),
    EventCommand.UNRECOGNISED: TranslatedTitle("unrecognised command", "нераспознанная команда"),
}
_IR_ADDRESS_GROUP_OFF = 0xFF
_IR_ADDRESS_GROUP_OFF_TITLE = TranslatedTitle("0 (off)", "0 (выкл.)")
# The code selector of the IR remote names the groups 1-9, A-E.
_IR_ADDRESS_GROUP_TITLES = {
    _IR_ADDRESS_GROUP_OFF: _IR_ADDRESS_GROUP_OFF_TITLE.en,
    **{group: f"{group:X}" for group in range(1, 15)},
}
_EVENT_TRANSLATIONS = {
    _IR_ADDRESS_GROUP_OFF_TITLE.en: _IR_ADDRESS_GROUP_OFF_TITLE.ru,
    "Target": "Адресат",
    "Short address": "Короткий адрес",
    "Group": "Группа",
    "Command": "Команда",
    "Scene": "Сцена",
    _LEVEL_TITLE.en: _LEVEL_TITLE.ru,
    "Fade Time, s": "Время изменения, с",
    "no fade": "мгновенно",
    **{
        title.en: title.ru
        for title in (
            _FADE_TIME_NOT_SET_TITLE,
            _FADE_TIME_HINT,
            *_TARGET_TITLES.values(),
            *_COMMAND_TITLES.values(),
        )
    },
}


class HelvarFirmware(NamedTuple):
    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor:02d}"


_SENSITIVITY_MIN_FIRMWARE = HelvarFirmware(6, 0)


@dataclass(frozen=True)
class HelvarIdentity:
    firmware: HelvarFirmware
    gtin: int
    serial_number: int

    @property
    def model(self) -> str:
        return HELVAR_MODEL_BY_GTIN.get(self.gtin, f"Helvar {self.gtin}")

    @property
    def has_settings(self) -> bool:
        return self.gtin in HELVAR_MODEL_BY_GTIN


def _any_model(identity: HelvarIdentity) -> bool:
    del identity
    return True


def _is_311_from_6_00(identity: HelvarIdentity) -> bool:
    return identity.gtin == HELVAR_311_GTIN and identity.firmware >= _SENSITIVITY_MIN_FIRMWARE


def _is_321(identity: HelvarIdentity) -> bool:
    return identity.gtin == HELVAR_321_GTIN


def _has_touch_switch(identity: HelvarIdentity) -> bool:
    return _is_311_from_6_00(identity) or _is_321(identity)


def _responds_to_groups(target: e2.Target) -> int:
    """The event follows the commands to the group it targets."""
    return 1 << target.group if isinstance(target, e2.Group) else 0


def _event_command(action: Optional[e2.Action]) -> EventCommand:
    if action is None:
        return EventCommand.UNRECOGNISED
    if isinstance(action, e2.FixedAction):
        return EventCommand[action.command.name]
    return _COMMAND_BY_ACTION_CLASS[type(action)]


def _takes_fade_time(command: EventCommand) -> bool:
    if command in (
        *_FADED_ACTION_CLASS_BY_COMMAND,
        *_SCENE_ACTION_CLASS_BY_COMMAND,
        EventCommand.DIRECT_LEVEL,
    ):
        return True
    return command.name in e2.FixedCommand.__members__ and e2.FixedCommand[command.name].takes_fade_time


def _fade_time_from_form(form_value: dict) -> Optional[int]:
    return None if form_value["fade_time"] == FADE_TIME_USE_DEVICE else form_value["fade_time"]


def _action_from_form(form_value: dict) -> Optional[e2.Action]:
    command = EventCommand(form_value["command"])
    if command is EventCommand.UNRECOGNISED:
        return None
    if command is EventCommand.SCENE_TOGGLE and form_value["scene"] not in e2.SceneToggle.SCENES:
        raise ValueError(f"Scene {form_value['scene']} cannot be toggled, only {e2.SceneToggle.SCENES}")
    if command in _SCENE_ACTION_CLASS_BY_COMMAND:
        return _SCENE_ACTION_CLASS_BY_COMMAND[command](
            fade_time=_fade_time_from_form(form_value), scene=form_value["scene"]
        )
    if command is EventCommand.DIRECT_LEVEL:
        return e2.DirectLevel(fade_time=_fade_time_from_form(form_value), level=form_value["level"])
    if command in _FADED_ACTION_CLASS_BY_COMMAND:
        return _FADED_ACTION_CLASS_BY_COMMAND[command](fade_time=_fade_time_from_form(form_value))
    if command in _UNFADED_ACTION_CLASS_BY_COMMAND:
        return _UNFADED_ACTION_CLASS_BY_COMMAND[command]()
    fixed_command = e2.FixedCommand[command.name]
    return e2.FixedAction(
        fixed_command, _fade_time_from_form(form_value) if fixed_command.takes_fade_time else None
    )


@dataclass(frozen=True)
class _SettingField:
    """A form field showing one E2 field."""

    key: str
    memory: DigidimE2
    title: TranslatedTitle
    available: Callable[[HelvarIdentity], bool] = _any_model
    description: Optional[TranslatedTitle] = None
    # json-editor `options.dependencies`: the field is shown only when these fields have these values.
    shown_when: Optional[dict[str, Any]] = None

    def to_form(self, image: DigidimImage) -> Any:
        return image[self.memory]

    def to_image(self, image: DigidimImage, form_value: Any) -> None:
        image[self.memory] = form_value

    def form_property(self, order: int, image: DigidimImage) -> dict:
        raise NotImplementedError


@dataclass(frozen=True)
class _NumberField(_SettingField):
    minimum: int = 0
    maximum: int = 0xFF

    def to_form(self, image: DigidimImage) -> int:
        # Out-of-range bytes are shown clamped.
        return min(max(image[self.memory], self.minimum), self.maximum)

    def form_property(self, order: int, image: DigidimImage) -> dict:
        del image
        return _integer_property(self.title.en, order, self.minimum, self.maximum)


@dataclass(frozen=True)
class _SliderField(_NumberField):
    def form_property(self, order: int, image: DigidimImage) -> dict:
        del image
        return {
            **_integer_property(self.title.en, order, self.minimum, self.maximum, grid_columns=12),
            "format": "range",
        }


@dataclass(frozen=True)
class _DurationField(_SettingField):
    minimum_s: int = e2.DURATION_STEP_S

    def to_form(self, image: DigidimImage) -> int:
        return max(image[self.memory], self.minimum_s)

    def form_property(self, order: int, image: DigidimImage) -> dict:
        del image
        return {
            **_integer_property(self.title.en, order, self.minimum_s, e2.DURATION_MAX_S, grid_columns=4),
            "description": _DURATION_STEP_HINT.en,
        }


@dataclass(frozen=True)
class _SwitchField(_SettingField):
    def form_property(self, order: int, image: DigidimImage) -> dict:
        del image
        return _switch_property(self.title.en, order)


@dataclass(frozen=True)
class _IrAddressGroupField(_SettingField):
    def form_property(self, order: int, image: DigidimImage) -> dict:
        titles = dict(_IR_ADDRESS_GROUP_TITLES)
        # A byte outside the offered codes is shown as it is.
        titles.setdefault(self.to_form(image), f"0x{self.to_form(image):02X}")
        return _enum_property(self.title.en, order, list(titles), list(titles.values()), grid_columns=12)


@dataclass(frozen=True)
class _EventField(_SettingField):
    offers_ramp: bool = False

    def to_form(self, image: DigidimImage) -> dict:
        event = image[self.memory]
        # Responds to Groups other than the target group make it an unrecognised command.
        action = event.action if event.responds_to_groups == _responds_to_groups(event.target) else None
        fade_time = getattr(action, "fade_time", None)
        return {
            "target": _TARGET_BY_CLASS[type(event.target)].value,
            "address": getattr(event.target, "address", 0),
            "group": getattr(event.target, "group", 0),
            "command": _event_command(action).value,
            "scene": getattr(action, "scene", 0),
            "level": getattr(action, "level", 0),
            "fade_time": FADE_TIME_USE_DEVICE if fade_time is None else fade_time,
        }

    def to_image(self, image: DigidimImage, form_value: Any) -> None:
        target_kind = EventTarget(form_value["target"])
        if target_kind is EventTarget.ADDRESS:
            target: e2.Target = e2.ShortAddress(form_value["address"])
        elif target_kind is EventTarget.GROUP:
            target = e2.Group(form_value["group"])
        else:
            target = e2.Broadcast()
        image[self.memory] = e2.Event(target, _action_from_form(form_value), _responds_to_groups(target))

    def form_property(self, order: int, image: DigidimImage) -> dict:
        shown = EventCommand(self.to_form(image)["command"])
        hidden = {EventCommand.UNRECOGNISED, *(() if self.offers_ramp else (EventCommand.RAMP,))} - {shown}
        return _event_property(
            self.title.en, order, [command for command in EventCommand if command not in hidden]
        )


# In the order of the form.
_SETTING_FIELDS: tuple[_SettingField, ...] = (
    _SliderField(
        "sensitivity",
        DigidimE2.SENSITIVITY,
        TranslatedTitle("Sensitivity", "Чувствительность"),
        _is_311_from_6_00,
        minimum=1,
        maximum=9,
    ),
    _DurationField(
        "on_time_s",
        DigidimE2.ON_TIME_S,
        TranslatedTitle("On time, s", "Время включения, с"),
        minimum_s=3 * e2.DURATION_STEP_S,
    ),
    _DurationField(
        "transition_s",
        DigidimE2.TRANSITION_S,
        TranslatedTitle("Transition, s", "Время перехода к выключению, с"),
    ),
    _DurationField(
        "exit_delay_s",
        DigidimE2.EXIT_DELAY_S,
        TranslatedTitle("Exit delay, s", "Пауза после ручного выключения, с"),
        minimum_s=3 * e2.DURATION_STEP_S,
    ),
    _NumberField(
        "scene_store_delay_s",
        DigidimE2.SCENE_STORE_DELAY_S,
        TranslatedTitle("Scene store delay, s", "Задержка сохранения сцены, с"),
        minimum=2,
        maximum=25,
    ),
    _SwitchField("lock", DigidimE2.LOCK, TranslatedTitle("Lock", "Блокировка")),
    _SwitchField(
        "presence_test_mode",
        DigidimE2.PRESENCE_TEST_MODE,
        TranslatedTitle("Presence test mode", "Режим проверки присутствия"),
        description=TranslatedTitle(
            "The exit delay is cut to about 20 s to test the presence sensor",
            "Пауза после ручного выключения сокращается примерно до 20 с, чтобы проверить датчик присутствия",
        ),
    ),
    _SwitchField(
        "presence_sensor", DigidimE2.PRESENCE_SENSOR, TranslatedTitle("Presence sensor", "Датчик присутствия")
    ),
    _SwitchField(
        "ir_receiver",
        DigidimE2.IR_RECEIVER,
        TranslatedTitle("IR remote receiver", "ИК-приёмник пульта"),
        _is_311_from_6_00,
    ),
    _SwitchField(
        "ir_disabled",
        DigidimE2.IR_DISABLED,
        TranslatedTitle("Ignore IR remote", "Игнорировать ИК-пульт"),
        _is_311_from_6_00,
    ),
    _IrAddressGroupField(
        "ir_address_group",
        DigidimE2.IR_ADDRESS_GROUP,
        TranslatedTitle("IR address group", "Код ИК-пульта"),
        _is_311_from_6_00,
        TranslatedTitle(
            "The position of the code selector on the IR remote the sensor responds to; needed when "
            "several sensors with IR are nearby",
            "Положение переключателя кода на ИК-пульте, на которое отвечает датчик; нужно, когда рядом "
            "несколько датчиков с ИК",
        ),
    ),
    _EventField(
        "presence_on_event",
        DigidimE2.PRESENCE_ON_EVENT,
        TranslatedTitle("Presence On event", "Событие при обнаружении движения"),
    ),
    _EventField(
        "presence_min_event",
        DigidimE2.PRESENCE_MIN_EVENT,
        TranslatedTitle("Presence Min event", "Событие по окончании времени включения"),
    ),
    _EventField(
        "presence_off_event",
        DigidimE2.PRESENCE_OFF_EVENT,
        TranslatedTitle("Presence Off event", "Событие по окончании перехода к выключению"),
    ),
    _SwitchField(
        "constant_light",
        DigidimE2.CONSTANT_LIGHT,
        TranslatedTitle("Constant light", "Поддержание освещённости"),
        _is_321,
        TranslatedTitle(
            "The sensor adjusts the level of the lamps set below to keep the light under it at the target "
            "level; commands to the target of the Presence Off event change that level",
            "Датчик подстраивает яркость светильников, заданных ниже, чтобы освещённость под ним держалась "
            "на целевом уровне; команды адресату события по окончании перехода к выключению меняют этот "
            "уровень",
        ),
    ),
    _EventField(
        "constant_light_event",
        DigidimE2.CONSTANT_LIGHT_EVENT,
        TranslatedTitle("Constant light lamps", "Светильники для поддержания освещённости"),
        _is_321,
        shown_when={"constant_light": True},
        offers_ramp=True,
    ),
    _EventField(
        "touch_switch_event",
        DigidimE2.TOUCH_SWITCH_EVENT,
        TranslatedTitle("Touch switch input", "Сенсорная кнопка"),
        _has_touch_switch,
    ),
    *(
        _EventField(
            f"ir_selection_{button}_event",
            DigidimE2[f"IR_SELECTION_{button}_EVENT"],
            TranslatedTitle(f"IR selection {button}", f"Кнопка сцены {button} ИК-пульта"),
            _is_311_from_6_00,
        )
        for button in range(1, 9)
    ),
    _EventField(
        "ir_wall_panel_right_event",
        DigidimE2.IR_WALL_PANEL_RIGHT_EVENT,
        TranslatedTitle("IR wall panel right", "Правая клавиша настенной ИК-панели"),
        _is_311_from_6_00,
    ),
    _EventField(
        "ir_wall_panel_left_event",
        DigidimE2.IR_WALL_PANEL_LEFT_EVENT,
        TranslatedTitle("IR wall panel left", "Левая клавиша настенной ИК-панели"),
        _is_311_from_6_00,
    ),
    _EventField(
        "ir_on_off_event",
        DigidimE2.IR_ON_OFF_EVENT,
        TranslatedTitle("IR On/Off", "Кнопка «вкл/выкл» ИК-пульта"),
        _is_311_from_6_00,
    ),
    _EventField(
        "ir_up_event",
        DigidimE2.IR_UP_EVENT,
        TranslatedTitle("IR Up", "Кнопка «ярче» ИК-пульта"),
        _is_311_from_6_00,
    ),
    _EventField(
        "ir_down_event",
        DigidimE2.IR_DOWN_EVENT,
        TranslatedTitle("IR Down", "Кнопка «темнее» ИК-пульта"),
        _is_311_from_6_00,
    ),
)


async def identify_helvar(
    driver: WBDALIDriver, address: GearShort, logger: logging.Logger
) -> Optional[HelvarIdentity]:
    """Identify the device; None if it is not Helvar. Ordinary gear gets only the major version query."""
    try:
        [major] = await query_responses_retry_only_failed(
            driver,
            [QueryMajorVersion(address)],
            logger,
            priority=FramePriority.CONFIGURATION,
        )
        [minor] = await query_responses_retry_only_failed(
            driver,
            [QueryMinorVersion(address)],
            logger,
            priority=FramePriority.CONFIGURATION,
        )
        image = DigidimImage()
        await image.read(driver, address, [DigidimE2.GTIN, DigidimE2.SERIAL_NUMBER], logger)
    except RuntimeError:
        return None
    digits = image[DigidimE2.GTIN]
    if not digits.isdigit() or not digits.startswith(HELVAR_GTIN_PREFIXES):
        return None
    return HelvarIdentity(
        HelvarFirmware(major.raw_value.as_integer, minor.raw_value.as_integer),
        int(digits),
        image[DigidimE2.SERIAL_NUMBER],
    )


def _integer_property(title: str, order: int, minimum: int, maximum: int, grid_columns: int = 6) -> dict:
    return {
        "type": "integer",
        "title": title,
        "minimum": minimum,
        "maximum": maximum,
        "propertyOrder": order,
        "options": {"grid_columns": grid_columns},
    }


def _switch_property(title: str, order: int) -> dict:
    return {
        "type": "boolean",
        "title": title,
        "format": "switch",
        "propertyOrder": order,
        "options": {"grid_columns": 12},
    }


def _enum_property(title: str, order: int, values: list, titles: list[str], grid_columns: int = 6) -> dict:
    return {
        "type": "integer" if isinstance(values[0], int) else "string",
        "title": title,
        "enum": values,
        "propertyOrder": order,
        "options": {"enum_titles": titles, "grid_columns": grid_columns},
    }


def _shown_when(form_property: dict, dependencies: dict[str, Any]) -> dict:
    return {**form_property, "options": {**form_property.get("options", {}), "dependencies": dependencies}}


def _event_property(title: str, order: int, commands: list[EventCommand]) -> dict:
    scene_commands = [command.value for command in commands if command in _SCENE_ACTION_CLASS_BY_COMMAND]
    faded_commands = [command.value for command in commands if _takes_fade_time(command)]
    return {
        "type": "object",
        "title": title,
        "format": "card",
        "propertyOrder": order,
        "required": ["target", "command"],
        "properties": {
            "target": _enum_property(
                "Target",
                1,
                [target.value for target in EventTarget],
                [_TARGET_TITLES[target].en for target in EventTarget],
            ),
            "address": _shown_when(
                _integer_property("Short address", 2, 0, MAX_SHORT_ADDRESS),
                {"target": EventTarget.ADDRESS.value},
            ),
            "group": _shown_when(
                _integer_property("Group", 3, 0, GROUPS_TOTAL - 1), {"target": EventTarget.GROUP.value}
            ),
            "command": _enum_property(
                "Command",
                4,
                [command.value for command in commands],
                [_COMMAND_TITLES[command].en for command in commands],
                grid_columns=12,
            ),
            "scene": _shown_when(
                _enum_property(
                    "Scene", 5, list(range(SCENES_TOTAL)), [str(scene) for scene in range(SCENES_TOTAL)]
                ),
                {"command": scene_commands},
            ),
            "level": _shown_when(
                _integer_property(_LEVEL_TITLE.en, 6, 0, MASK - 1),
                {"command": EventCommand.DIRECT_LEVEL.value},
            ),
            "fade_time": {
                **_shown_when(
                    _enum_property(
                        "Fade Time, s",
                        7,
                        [FADE_TIME_USE_DEVICE, *range(FADE_TIME_CODE_MAX + 1)],
                        [_FADE_TIME_NOT_SET_TITLE.en, *FADE_TIME_ENUM_TITLES],
                    ),
                    {"command": faded_commands},
                ),
                "description": _FADE_TIME_HINT.en,
            },
        },
    }


class HelvarSettings:
    """Settings of one 311 or 321; the fields depend on the model and its firmware."""

    def __init__(self, identity: HelvarIdentity) -> None:
        self._fields = [field for field in _SETTING_FIELDS if field.available(identity)]
        self._image: Optional[DigidimImage] = None

    async def read(self, driver: WBDALIDriver, address: GearShort, logger: logging.Logger) -> None:
        image = DigidimImage()
        await image.read(driver, address, [field.memory for field in self._fields], logger)
        self._image = image

    def add_to_form(self, params: dict, schema: dict) -> None:
        params.update(self._form_values())
        schema.setdefault("properties", {}).update(self._properties())
        translations = schema.setdefault("translations", {}).setdefault("ru", {})
        translations.update(_EVENT_TRANSLATIONS)
        translations[_DURATION_STEP_HINT.en] = _DURATION_STEP_HINT.ru
        for field in self._fields:
            translations[field.title.en] = field.title.ru
            if field.description is not None:
                translations[field.description.en] = field.description.ru

    async def write(
        self, driver: WBDALIDriver, address: GearShort, new_values: dict, logger: logging.Logger
    ) -> None:
        """Write the fields whose value differs from the one the form showed; others keep their bits."""
        if self._image is None:
            await self.read(driver, address, logger)
        shown = self._form_values()
        try:
            for field in self._fields:
                if field.key in new_values and new_values[field.key] != shown[field.key]:
                    field.to_image(self._image, new_values[field.key])
        except Exception:
            self._image.drop_changes()
            raise
        try:
            await self._image.write(driver, address, logger)
        except Exception:
            # A run may be written in part, so the image is read again before the next write.
            self._image = None
            raise

    # --- Private ---

    def _form_values(self) -> dict:
        return {field.key: field.to_form(self._image) for field in self._fields}

    def _properties(self) -> dict:
        properties = {
            field.key: field.form_property(_PROPERTY_ORDER + _SETTING_FIELDS.index(field), self._image)
            for field in self._fields
        }
        for field in self._fields:
            if field.description is not None:
                properties[field.key]["description"] = field.description.en
            if field.shown_when is not None:
                properties[field.key] = _shown_when(properties[field.key], field.shown_when)
        return properties


class HelvarStatusControl(SingleQueryControl):
    """`error_status` of a sensor: an alarm while it does not answer QUERY CONTROL GEAR PRESENT."""

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                "error_status",
                ControlState(ControlMeta(ALARM_CONTROL_TYPE, _STATUS_OK_TITLE, read_only=True), "0"),
            ),
            query_builder=QueryControlGearPresent,
            poll_interval=PERIODIC_STATUS_POLL_INTERVAL,
        )

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, GearPresentRead):
            return NotifyResult.NOTHING_TO_PUBLISH
        if event.failed:
            self.control_info.state.set_read_error()
            return NotifyResult.PUBLISH_STATE
        answers = bool(event.response.value)
        self.control_info.state.value = "0" if answers else "1"
        self.control_info.state.meta.title = _STATUS_OK_TITLE if answers else _STATUS_NOT_RESPONDING_TITLE
        self.control_info.state.clear_errors()
        return NotifyResult.PUBLISH_STATE

    # --- Hooks for subclasses ---

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        return GearPresentRead(response, response is None)


class HelvarDigidimSensor(DaliDevice):
    """A Digidim sensor in the control gear space."""

    def __init__(  # pylint: disable=too-many-arguments, too-many-positional-arguments
        self,
        address: DaliDeviceAddress,
        bus_id: str,
        gtin_db: DaliDatabase,
        common_schema: dict,
        mqtt_id: Optional[str] = None,
        name: Optional[str] = None,
        on_off: Optional[OnOffConfig] = None,
    ) -> None:
        super().__init__(address, bus_id, gtin_db, common_schema, mqtt_id, name, on_off)
        self._settings: Optional[HelvarSettings] = None

    async def load_info(self, driver: WBDALIDriver, force_reload: bool = False) -> None:
        if self.params and not force_reload:
            return
        identity = await identify_helvar(driver, GearShort(self.address.short), self.logger)
        if identity is None:
            raise RuntimeError(f"Device {self.name} does not answer as a Helvar Digidim device")
        self._settings = HelvarSettings(identity) if identity.has_settings else None
        if self._settings is not None:
            await self._settings.read(driver, GearShort(self.address.short), self.logger)
        # pylint: disable=duplicate-code
        params = {
            "short_address": self.address.short,
            "random_address": hex(self.address.random),
            "name": self.name,
            "mqtt_id": self.mqtt_id,
            "gtin": identity.gtin,
            "identification_number": f"{identity.serial_number:09d}",
            "firmware_version": str(identity.firmware),
            "brand_name": HELVAR_BRAND_NAME,
        }
        # pylint: enable=duplicate-code
        schema = deepcopy(self._common_schema)
        note = _UNSUPPORTED_MODEL_NOTE if self._settings is None else _SENSOR_NOTE
        schema["description"] = note.en
        schema.setdefault("translations", {}).setdefault("ru", {})[note.en] = note.ru
        if self._settings is not None:
            params["product_name"] = identity.model
            self._settings.add_to_form(params, schema)
        self.params = params
        self.schema = schema

    async def apply_parameters(self, driver: WBDALIDriver, new_values: dict) -> ApplyResult:
        if not self.params:
            await self.load_info(driver)
        jsonschema.validate(
            instance=new_values, schema=self.schema, format_checker=jsonschema.draft4_format_checker
        )
        if self._settings is not None:
            await self._settings.write(driver, GearShort(self.address.short), new_values, self.logger)
            self._settings.add_to_form(self.params, self.schema)
        await self._apply_common_parameters(driver, new_values)
        return ApplyResult()

    async def identify(self, driver: WBDALIDriver) -> None:
        await send_with_retry(driver, Initialise(address=self.address.short), self.logger)
        try:
            await asyncio.sleep(IDENTIFY_INITIALISED_TIME_S)
        finally:
            await send_with_retry(driver, Terminate(), self.logger)

    async def reset_settings(self, driver: WBDALIDriver) -> None:
        """Nothing: the settings live in E2, DALI RESET is not sent to Helvar sensors."""

    # --- Hooks for subclasses ---

    def _build_mqtt_controls(self) -> list[MqttControlBase]:
        return [HelvarStatusControl()]

    async def _initialize_impl(self, driver: WBDALIDriver) -> InitializationResult:
        mqtt_controls = self._build_mqtt_controls()
        await self._read_initial_control_state(driver, mqtt_controls)
        return InitializationResult([], [], mqtt_controls)
