from typing import Callable, Optional, Union

from dali.address import GearBroadcast, GearGroup, GearShort
from dali.command import Command, Response
from dali.gear.general import (
    DAPC,
    Down,
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

from .common_dali_device import MqttControl, MqttControlBase
from .dali_common_parameters import SCENES_TOTAL
from .dali_dimming_curve import DimmingCurveState
from .device_publisher import ControlInfo
from .wbmqtt import ControlMeta, TranslatedTitle

AddressFactory = Callable[[int], Union[GearBroadcast, GearGroup, GearShort]]


def _build_error_status_query(short_address: int) -> QueryStatus:
    return QueryStatus(GearShort(short_address))


def _format_error_status(response: Response) -> str:
    if not getattr(response, "error", False):
        return "OK"

    details: list[str] = []
    if getattr(response, "ballast_status", False):
        details.append("ballast not ok")
    if getattr(response, "lamp_failure", False):
        details.append("lamp failure")
    if getattr(response, "missing_short_address", False):
        details.append("missing short address")

    return ", ".join(details)


def _make_handle_dapc(addr: AddressFactory):
    def handle_dapc(short_address: int, value: str) -> list[Command]:
        try:
            power = int(value, 0)
        except ValueError:
            power = value
        return [DAPC(addr(short_address), power)]

    return handle_dapc


class ActualLevelControl(MqttControlBase):
    def __init__(self, dimming_curve_state: DimmingCurveState) -> None:
        super().__init__(
            ControlInfo(
                "actual_level",
                ControlMeta(
                    title=TranslatedTitle("Actual Level", "Фактический уровень"),
                    read_only=True,
                    units="%",
                ),
                "0",
            )
        )
        self._dimming_curve_state = dimming_curve_state

    def get_query(self, short_address: int) -> Optional[Command]:
        return QueryActualLevel(GearShort(short_address))

    def format_response(self, response: Response) -> str:
        return f"{self._dimming_curve_state.get_level(response.raw_value.as_integer):.3f}"

    def is_readable(self) -> bool:
        return True


def make_controls(addr: AddressFactory) -> list[MqttControlBase]:
    return [
        MqttControl(
            ControlInfo("off", ControlMeta("pushbutton", TranslatedTitle("Off", "Выкл"))),
            commands_builder=lambda short_address, _, _addr=addr: [Off(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo("up", ControlMeta("pushbutton", TranslatedTitle("Up", "Вверх"))),
            commands_builder=lambda short_address, _, _addr=addr: [Up(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo("down", ControlMeta("pushbutton", TranslatedTitle("Up", "Вниз"))),
            commands_builder=lambda short_address, _, _addr=addr: [Down(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo("step_up", ControlMeta("pushbutton", TranslatedTitle("Step Up", "Шаг вверх"))),
            commands_builder=lambda short_address, _, _addr=addr: [StepUp(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo("step_down", ControlMeta("pushbutton", TranslatedTitle("Step Up", "Шаг вниз"))),
            commands_builder=lambda short_address, _, _addr=addr: [StepDown(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo(
                "recall_max_level",
                ControlMeta("pushbutton", TranslatedTitle("Recall Max Level", "Максимальный уровень")),
            ),
            commands_builder=lambda short_address, _, _addr=addr: [RecallMaxLevel(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo(
                "recall_min_level",
                ControlMeta("pushbutton", TranslatedTitle("Recall Min Level", "Минимальный уровень")),
            ),
            commands_builder=lambda short_address, _, _addr=addr: [RecallMinLevel(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo(
                "step_down_and_off",
                ControlMeta("pushbutton", TranslatedTitle("Step Down And Off", "Шаг вниз и выкл")),
            ),
            commands_builder=lambda short_address, _, _addr=addr: [StepDownAndOff(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo(
                "on_and_step_up",
                ControlMeta("pushbutton", TranslatedTitle("On And Step Up", "Вкл и шаг вверх")),
            ),
            commands_builder=lambda short_address, _, _addr=addr: [OnAndStepUp(_addr(short_address))],
        ),
        MqttControl(
            ControlInfo(
                "dapc",
                ControlMeta("text", TranslatedTitle("Direct Arc Power Control", "Задать мощность (DAPC)")),
                "",
            ),
            commands_builder=_make_handle_dapc(addr),
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
            commands_builder=lambda short_address, value, _addr=addr: [
                GoToScene(_addr(short_address), int(value, 0))
            ],
        ),
    ]


CONTROLS: list[MqttControlBase] = [
    MqttControl(
        ControlInfo(
            "error_status",
            ControlMeta("alarm", TranslatedTitle("Error Status", "Статус ошибки"), read_only=True),
            "0",
        ),
        query_builder=_build_error_status_query,
        value_formatter=_format_error_status,
    ),
    *make_controls(GearShort),
]
