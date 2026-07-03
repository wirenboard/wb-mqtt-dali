import math
from typing import Callable, Optional, Protocol, Union

from dali.address import Address, GearBroadcast, GearGroup, GearShort
from dali.command import Command, Response
from dali.gear.general import (
    DAPC,
    Down,
    GoToLastActiveLevel,
    GoToScene,
    Off,
    OnAndStepUp,
    QueryActualLevel,
    QueryStatus,
    RecallMaxLevel,
    RecallMinLevel,
    StepDown,
    StepDownAndOff,
    StepUp,
    Up,
)

from .common_dali_device import EVENT_RESYNC_BASE_INTERVAL, MqttControl, MqttControlBase
from .control_ids import ACTUAL_LEVEL
from .control_ids import DAPC as DAPC_ID
from .control_ids import WANTED_LEVEL
from .dali_common_parameters import SCENES_TOTAL, MaxLevelParam, MinLevelParam
from .dali_dimming_curve import DimmingCurveState
from .device_publisher import ControlInfo
from .wbdali_utils import MASK
from .wbmqtt import ControlMeta, TranslatedTitle

AddressFactory = Callable[[int], Union[GearBroadcast, GearGroup, GearShort]]


class SceneLevelSource(Protocol):  # pylint: disable=too-few-public-methods
    """Reports a raw scene level. Implemented by two unrelated classes chosen per device
    type — the gear ``ScenesParam`` and the DT8 ``ScenesSettings`` — so a structural
    protocol is what unifies them without a shared base or a cross-module import."""

    def scene_level(self, index: int) -> Optional[int]:
        """Raw scene level for ``index``, or ``None`` if not known / scene disabled."""


def handle_dapc(short_address: Address, value: str) -> list[Command]:
    try:
        power = int(value, 0)
    except ValueError:
        power = value
    return [DAPC(short_address, power)]


class ActualLevelControl(MqttControlBase):
    is_group_state_control = True

    def __init__(
        self,
        dimming_curve_state: DimmingCurveState,
        max_level: Optional[MaxLevelParam] = None,
        min_level: Optional[MinLevelParam] = None,
        scene_source: Optional[SceneLevelSource] = None,
    ) -> None:
        super().__init__(
            ControlInfo(
                ACTUAL_LEVEL,
                ControlMeta(
                    title=TranslatedTitle("Actual Level", "Яркость"),
                    read_only=True,
                    units="%",
                ),
                "0",
            ),
            poll_interval=EVENT_RESYNC_BASE_INTERVAL,
            randomize_poll_interval=True,
        )
        self._dimming_curve_state = dimming_curve_state
        self._max_level = max_level
        self._min_level = min_level
        self._scene_source = scene_source
        # Typed prediction state: last known raw level.
        self._level: Optional[int] = None

    @property
    def current_level(self) -> Optional[int]:
        return self._level

    def get_query(self, short_address: Address) -> Optional[Command]:
        return QueryActualLevel(short_address)

    def format_response(self, response: Response) -> str:
        raw = response.raw_value.as_integer
        if raw <= 254:
            self._level = raw
        return self._format_level(raw)

    def is_readable(self) -> bool:
        return True

    def apply(self, command: Command) -> Optional[str]:
        """Predict the new level from a sniffed/own level command.

        Returns the published ``%`` string, or ``None`` when the effect is not
        predictable (poll only). Reads MAX/MIN/scene from injected owner params.
        """
        new_level = self._predict_level(command)
        if new_level is None:
            return None
        self._level = new_level
        value = self._format_level(new_level)
        self.control_info.value = value
        return value

    # --- Private ---

    def _format_level(self, raw: int) -> str:
        return f"{self._dimming_curve_state.get_level(raw):.3f}"

    def _max(self) -> Optional[int]:
        return self._max_level.value if self._max_level is not None else None

    def _min(self) -> Optional[int]:
        return self._min_level.value if self._min_level is not None else None

    def _predict_level(self, command: Command) -> Optional[int]:
        if isinstance(command, (StepUp, StepDown, StepDownAndOff, OnAndStepUp)):
            return self._predict_step(command)
        if isinstance(command, DAPC):
            # 255 (MASK) = stop fade / no change; 0 = off; else the target level.
            return None if command.power == MASK else command.power
        if isinstance(command, Off):
            return 0
        if isinstance(command, GoToScene):
            return self._scene_level(command.param)
        # GoToLastActiveLevel (rarely emitted, would need last-active tracking), Recall
        # max/min, Up/Down and anything else are not predicted here.
        return self._predict_recall(command)

    def _predict_recall(self, command: Command) -> Optional[int]:
        if isinstance(command, RecallMaxLevel):
            return self._max()
        if isinstance(command, RecallMinLevel):
            return self._min()
        return None

    def _scene_level(self, index: int) -> Optional[int]:
        return self._scene_source.scene_level(index) if self._scene_source is not None else None

    def _predict_step(self, command: Command) -> Optional[int]:
        cur = self._level
        if cur is None:
            return None
        maximum = self._max()
        minimum = self._min()
        if isinstance(command, StepUp):
            return 0 if cur == 0 else self._step_up(cur, maximum)
        if isinstance(command, StepDown):
            return 0 if cur == 0 else (None if minimum is None else max(cur - 1, minimum))
        if isinstance(command, StepDownAndOff):
            return None if minimum is None else (0 if cur <= minimum else cur - 1)
        # OnAndStepUp: from off, go to MIN; otherwise step up toward MAX.
        return minimum if cur == 0 else self._step_up(cur, maximum)

    @staticmethod
    def _step_up(cur: int, maximum: Optional[int]) -> Optional[int]:
        return None if maximum is None else min(cur + 1, maximum)


class WantedLevelControl(MqttControlBase):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__(
            ControlInfo(
                WANTED_LEVEL,
                ControlMeta(
                    "range",
                    title=TranslatedTitle("Wanted Level", "Желаемая яркость"),
                    units="%",
                    minimum=0,
                    maximum=100,
                ),
                "0",
            )
        )
        self._dimming_curve_state = dimming_curve_state

    def get_setup_commands(self, short_address: Address, value_to_set: str) -> list[Command]:
        try:
            level_in_percent = float(value_to_set)
        except ValueError as exc:
            raise ValueError("Level must be a number between 0 and 100") from exc
        if not math.isfinite(level_in_percent) or level_in_percent < 0 or level_in_percent > 100:
            raise ValueError("Level must be a number between 0 and 100")
        level = self._dimming_curve_state.get_raw_value(level_in_percent)
        return [DAPC(short_address, level)]

    def is_writable(self) -> bool:
        return True


def make_controls() -> list[MqttControlBase]:
    return [
        MqttControl(
            ControlInfo(
                DAPC_ID,
                ControlMeta(
                    "value",
                    TranslatedTitle("Direct Arc Power Control", "Прямое управление яркостью"),
                    minimum=0,
                    maximum=254,
                ),
                "0",
            ),
            commands_builder=handle_dapc,
        ),
        MqttControl(
            ControlInfo(
                "go_to_last_active_level",
                ControlMeta(
                    "pushbutton",
                    TranslatedTitle("Last Active Level", "Последняя активная яркость"),
                ),
            ),
            commands_builder=lambda short_address, _: [GoToLastActiveLevel(short_address)],
        ),
        MqttControl(
            ControlInfo("off", ControlMeta("pushbutton", TranslatedTitle("Off", "Выкл"))),
            commands_builder=lambda short_address, _: [Off(short_address)],
        ),
        MqttControl(
            ControlInfo("up", ControlMeta("pushbutton", TranslatedTitle("Up", "Вверх"))),
            commands_builder=lambda short_address, _: [Up(short_address)],
        ),
        MqttControl(
            ControlInfo("down", ControlMeta("pushbutton", TranslatedTitle("Down", "Вниз"))),
            commands_builder=lambda short_address, _: [Down(short_address)],
        ),
        MqttControl(
            ControlInfo("step_up", ControlMeta("pushbutton", TranslatedTitle("Step Up", "Шаг вверх"))),
            commands_builder=lambda short_address, _: [StepUp(short_address)],
        ),
        MqttControl(
            ControlInfo("step_down", ControlMeta("pushbutton", TranslatedTitle("Step Down", "Шаг вниз"))),
            commands_builder=lambda short_address, _: [StepDown(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "recall_max_level",
                ControlMeta("pushbutton", TranslatedTitle("Recall Max Level", "Максимальная яркость")),
            ),
            commands_builder=lambda short_address, _: [RecallMaxLevel(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "recall_min_level",
                ControlMeta("pushbutton", TranslatedTitle("Recall Min Level", "Минимальная яркость")),
            ),
            commands_builder=lambda short_address, _: [RecallMinLevel(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "step_down_and_off",
                ControlMeta("pushbutton", TranslatedTitle("Step Down And Off", "Шаг вниз и выкл")),
            ),
            commands_builder=lambda short_address, _: [StepDownAndOff(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "on_and_step_up",
                ControlMeta("pushbutton", TranslatedTitle("On And Step Up", "Вкл и шаг вверх")),
            ),
            commands_builder=lambda short_address, _: [OnAndStepUp(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "go_to_scene",
                ControlMeta(
                    title=TranslatedTitle("Go To Scene", "Перейти к сцене"),
                    enum={str(i): TranslatedTitle() for i in range(SCENES_TOTAL)},
                ),
                "0",
            ),
            commands_builder=lambda short_address, value: [GoToScene(short_address, int(value, 0))],
        ),
    ]


class ErrorStatusControl(MqttControlBase):

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                "error_status",
                ControlMeta("alarm", TranslatedTitle("Ok", "Норма"), read_only=True),
                "0",
            ),
            poll_interval=120.0,
        )

    def get_query(self, short_address: Address) -> Optional[Command]:
        return QueryStatus(short_address)

    def is_readable(self) -> bool:
        return True

    def format_response(self, response: Response) -> str:
        return "1" if getattr(response, "error", False) else "0"

    def format_title(self, response: Response) -> TranslatedTitle:
        if not getattr(response, "error", False):
            return TranslatedTitle("Ok", "Норма")

        details: list[str] = []
        details_ru: list[str] = []
        if getattr(response, "ballast_status", False):
            details.append("Ballast not ok")
            details_ru.append("Ошибка балласта")
        if getattr(response, "lamp_failure", False):
            details.append("Lamp failure")
            details_ru.append("Неисправность лампы")
        if getattr(response, "missing_short_address", False):
            details.append("Missing short address")
            details_ru.append("Отсутствует короткий адрес")

        return TranslatedTitle(", ".join(details), ", ".join(details_ru))
