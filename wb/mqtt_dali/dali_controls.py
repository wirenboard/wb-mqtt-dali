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

from .common_dali_device import (
    EVENT_RESYNC_BASE_INTERVAL,
    PERIODIC_STATUS_POLL_INTERVAL,
    MqttControl,
    MqttControlBase,
    NotifyResult,
    SingleQueryControl,
)
from .control_ids import ACTUAL_LEVEL
from .control_ids import DAPC as DAPC_ID
from .control_ids import WANTED_LEVEL
from .dali_common_parameters import SCENES_TOTAL, MaxLevelParam, MinLevelParam
from .dali_dimming_curve import DimmingCurveState
from .device_publisher import ControlInfo
from .events import BusEvent, EventSource, LevelChanged, StatusRead
from .wbdali_utils import MASK
from .wbmqtt import (
    ALARM_CONTROL_TYPE,
    ControlError,
    ControlMeta,
    ControlState,
    TranslatedTitle,
)

AddressFactory = Callable[[int], Union[GearBroadcast, GearGroup, GearShort]]

# Highest raw gear level; MASK (255) above it means "level unknown", not a level.
MAX_GEAR_LEVEL = MASK - 1


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


class ActualLevelControl(SingleQueryControl):
    is_group_state_control = True
    follows_device_fade = True

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
                ControlState(
                    ControlMeta(
                        title=TranslatedTitle("Actual Level", "Яркость"),
                        read_only=True,
                        units="%",
                    ),
                    "0",
                ),
            ),
            query_builder=QueryActualLevel,
            poll_interval=EVENT_RESYNC_BASE_INTERVAL,
            randomize_poll_interval=True,
        )
        self._dimming_curve_state = dimming_curve_state
        self._max_level = max_level
        self._min_level = min_level
        self._scene_source = scene_source
        self._level: Optional[int] = None

    @property
    def current_level(self) -> Optional[int]:
        return self._level

    def predict_level(self, command: Command) -> Optional[int]:
        """Predict the raw level a sniffed/own level command would produce.

        ``None`` when the effect is not predictable (poll only).
        """
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

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, LevelChanged):
            return super().notify(event)
        if event.settle_at is not None:
            self.schedule_poll_at(event.settle_at)
        if event.failed:
            self.control_info.state.error = ControlError.READ
            return NotifyResult.PUBLISH_STATE
        # Applying a predicted value while our own read is failing would clear /meta/error=r.
        if event.source is EventSource.OBSERVED and self.control_info.state.error:
            return NotifyResult.NOTHING_TO_PUBLISH
        # MASK is shown like any level but is none: step prediction keeps the last real one.
        if event.raw_level <= MAX_GEAR_LEVEL:
            self._level = event.raw_level
        self.control_info.state.value = self._format_level(event.raw_level)
        self.control_info.state.error = ControlError.NONE
        return NotifyResult.PUBLISH_STATE

    # --- Hooks for subclasses ---

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        if response is None:
            return LevelChanged(None, EventSource.READ, failed=True)
        return LevelChanged(response.raw_value.as_integer, EventSource.READ)

    # --- Private ---

    def _format_level(self, raw: int) -> str:
        return f"{self._dimming_curve_state.get_level(raw):.3f}"

    def _max(self) -> Optional[int]:
        return self._max_level.value if self._max_level is not None else None

    def _min(self) -> Optional[int]:
        return self._min_level.value if self._min_level is not None else None

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
                ControlState(
                    ControlMeta(
                        "range",
                        title=TranslatedTitle("Wanted Level", "Желаемая яркость"),
                        units="%",
                        minimum=0,
                        maximum=100,
                    ),
                    "0",
                ),
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

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, LevelChanged):
            return NotifyResult.NOTHING_TO_PUBLISH
        # A setpoint is "requested", not "measured": no read error to suppress a prediction
        # on, and MASK is a level like any other.
        if event.raw_level is None:
            return NotifyResult.NOTHING_TO_PUBLISH
        percent = self._dimming_curve_state.get_level(event.raw_level)
        self.control_info.state.value = str(round(percent))
        return NotifyResult.PUBLISH_STATE


class DapcControl(MqttControlBase):
    """DAPC setpoint: the raw-level representation of the level triplet's observed truth."""

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                DAPC_ID,
                ControlState(
                    ControlMeta(
                        "value",
                        TranslatedTitle("Direct Arc Power Control", "Прямое управление яркостью"),
                        minimum=0,
                        maximum=254,
                    ),
                    "0",
                ),
            )
        )

    def get_setup_commands(self, short_address: Address, value_to_set: str) -> list[Command]:
        return handle_dapc(short_address, value_to_set)

    def is_writable(self) -> bool:
        return True

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, LevelChanged):
            return NotifyResult.NOTHING_TO_PUBLISH
        if event.raw_level is None:
            return NotifyResult.NOTHING_TO_PUBLISH
        self.control_info.state.value = str(event.raw_level)
        return NotifyResult.PUBLISH_STATE


def make_controls() -> list[MqttControlBase]:
    return [
        DapcControl(),
        MqttControl(
            ControlInfo(
                "go_to_last_active_level",
                ControlState(
                    ControlMeta(
                        "pushbutton",
                        TranslatedTitle("Last Active Level", "Последняя активная яркость"),
                    )
                ),
            ),
            commands_builder=lambda short_address, _: [GoToLastActiveLevel(short_address)],
        ),
        MqttControl(
            ControlInfo("off", ControlState(ControlMeta("pushbutton", TranslatedTitle("Off", "Выкл")))),
            commands_builder=lambda short_address, _: [Off(short_address)],
        ),
        MqttControl(
            ControlInfo("up", ControlState(ControlMeta("pushbutton", TranslatedTitle("Up", "Вверх")))),
            commands_builder=lambda short_address, _: [Up(short_address)],
        ),
        MqttControl(
            ControlInfo("down", ControlState(ControlMeta("pushbutton", TranslatedTitle("Down", "Вниз")))),
            commands_builder=lambda short_address, _: [Down(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "step_up", ControlState(ControlMeta("pushbutton", TranslatedTitle("Step Up", "Шаг вверх")))
            ),
            commands_builder=lambda short_address, _: [StepUp(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "step_down", ControlState(ControlMeta("pushbutton", TranslatedTitle("Step Down", "Шаг вниз")))
            ),
            commands_builder=lambda short_address, _: [StepDown(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "recall_max_level",
                ControlState(
                    ControlMeta("pushbutton", TranslatedTitle("Recall Max Level", "Максимальная яркость"))
                ),
            ),
            commands_builder=lambda short_address, _: [RecallMaxLevel(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "recall_min_level",
                ControlState(
                    ControlMeta("pushbutton", TranslatedTitle("Recall Min Level", "Минимальная яркость"))
                ),
            ),
            commands_builder=lambda short_address, _: [RecallMinLevel(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "step_down_and_off",
                ControlState(
                    ControlMeta("pushbutton", TranslatedTitle("Step Down And Off", "Шаг вниз и выкл"))
                ),
            ),
            commands_builder=lambda short_address, _: [StepDownAndOff(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "on_and_step_up",
                ControlState(ControlMeta("pushbutton", TranslatedTitle("On And Step Up", "Вкл и шаг вверх"))),
            ),
            commands_builder=lambda short_address, _: [OnAndStepUp(short_address)],
        ),
        MqttControl(
            ControlInfo(
                "go_to_scene",
                ControlState(
                    ControlMeta(
                        title=TranslatedTitle("Go To Scene", "Перейти к сцене"),
                        enum={str(i): TranslatedTitle() for i in range(SCENES_TOTAL)},
                    ),
                    "0",
                ),
            ),
            commands_builder=lambda short_address, value: [GoToScene(short_address, int(value, 0))],
        ),
    ]


class ErrorStatusControl(SingleQueryControl):

    def __init__(self) -> None:
        super().__init__(
            ControlInfo(
                "error_status",
                ControlState(
                    ControlMeta(ALARM_CONTROL_TYPE, TranslatedTitle("Ok", "Норма"), read_only=True), "0"
                ),
            ),
            query_builder=QueryStatus,
            poll_interval=PERIODIC_STATUS_POLL_INTERVAL,
        )

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, StatusRead):
            return NotifyResult.NOTHING_TO_PUBLISH
        if event.failed:
            self.control_info.state.error = ControlError.READ
            return NotifyResult.PUBLISH_STATE
        self.control_info.state.value = self.format_response(event.response)
        # The title spells out which status bits are set, so a read changes it with the value.
        self.control_info.state.meta.title = self.format_title(event.response)
        self.control_info.state.error = ControlError.NONE
        return NotifyResult.PUBLISH_STATE

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

    # --- Hooks for subclasses ---

    def decode_response(self, response: Optional[Response]) -> BusEvent:
        return StatusRead(response, response is None)
