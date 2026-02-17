from dali.address import GearShort
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

from .common_dali_device import MqttControl
from .dali_common_parameters import SCENES_TOTAL
from .device_publisher import ControlInfo
from .wbmqtt import ControlMeta, TranslatedTitle


def _build_actual_level_query(short_address: int) -> QueryActualLevel:
    return QueryActualLevel(GearShort(short_address))


def _format_actual_level(response: Response) -> str:
    return str(response.raw_value.as_integer)


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


POLLING_CONTROLS: list[MqttControl] = [
    MqttControl(
        ControlInfo("actual_level", ControlMeta(title="Actual Level", read_only=True), "0"),
        query_builder=_build_actual_level_query,
        value_formatter=_format_actual_level,
    ),
    MqttControl(
        ControlInfo("error_status", ControlMeta("alarm", "Error Status", read_only=True), "0"),
        query_builder=_build_error_status_query,
        value_formatter=_format_error_status,
    ),
]


def handle_dapc(short_address: int, value: str) -> list[Command]:
    try:
        power = int(value, 0)
    except ValueError:
        power = value

    return [DAPC(GearShort(short_address), power)]


ACTION_CONTROLS: list[MqttControl] = [
    MqttControl(
        ControlInfo("off", ControlMeta("pushbutton", "Off")),
        commands_builder=lambda short_address, _: [Off(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("up", ControlMeta("pushbutton", "Up")),
        commands_builder=lambda short_address, _: [Up(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("down", ControlMeta("pushbutton", "Down")),
        commands_builder=lambda short_address, _: [Down(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("step_up", ControlMeta("pushbutton", "Step Up")),
        commands_builder=lambda short_address, _: [StepUp(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("step_down", ControlMeta("pushbutton", "Step Down")),
        commands_builder=lambda short_address, _: [StepDown(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("recall_max_level", ControlMeta("pushbutton", "Recall Max Level")),
        commands_builder=lambda short_address, _: [RecallMaxLevel(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("recall_min_level", ControlMeta("pushbutton", "Recall Min Level")),
        commands_builder=lambda short_address, _: [RecallMinLevel(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("step_down_and_off", ControlMeta("pushbutton", "Step Down And Off")),
        commands_builder=lambda short_address, _: [StepDownAndOff(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("on_and_step_up", ControlMeta("pushbutton", "On And Step Up")),
        commands_builder=lambda short_address, _: [OnAndStepUp(GearShort(short_address))],
    ),
    MqttControl(
        ControlInfo("dapc", ControlMeta("text", "Direct Arc Power Control"), ""),
        commands_builder=handle_dapc,
    ),
    MqttControl(
        ControlInfo(
            "go_to_scene",
            ControlMeta(
                "Go To Scene",
                enum={str(i): TranslatedTitle(str(i)) for i in range(SCENES_TOTAL)},
            ),
            "0",
        ),
        commands_builder=lambda short_address, value: [GoToScene(GearShort(short_address), int(value, 0))],
    ),
]
