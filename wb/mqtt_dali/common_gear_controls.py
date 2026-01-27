import asyncio
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from dali.address import GearShort
from dali.command import Response
from dali.gear.general import (
    Down,
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

from .dali_device import DaliDevice
from .device_publisher import ControlInfo, DevicePublisher


@dataclass(frozen=True)
class PollingControl:
    control_id: str
    title: str
    default_value: str
    query_builder: Callable[[DaliDevice], object]
    value_formatter: Callable[[Response], str]
    control_type: str = "value"


def _build_actual_level_query(device: DaliDevice) -> QueryActualLevel:
    return QueryActualLevel(GearShort(device.address.short))


def _format_actual_level(response: Response) -> str:
    return str(response.raw_value.as_integer)


def _build_error_status_query(device: DaliDevice) -> QueryStatus:
    return QueryStatus(GearShort(device.address.short))


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


POLLING_CONTROLS: tuple[PollingControl, ...] = (
    PollingControl(
        control_id="actual_level",
        title="Actual Level",
        default_value="0",
        query_builder=_build_actual_level_query,
        value_formatter=_format_actual_level,
        control_type="value",
    ),
    PollingControl(
        control_id="error_status",
        title="Error Status",
        default_value="0",
        query_builder=_build_error_status_query,
        value_formatter=_format_error_status,
        control_type="alarm",
    ),
)


def get_polling_control_count() -> int:
    return len(POLLING_CONTROLS)


PUSHBUTTON_CONTROLS: list[ControlInfo] = [
    ControlInfo("off", "Off", "pushbutton"),
    ControlInfo("up", "Up", "pushbutton"),
    ControlInfo("down", "Down", "pushbutton"),
    ControlInfo("step_up", "Step Up", "pushbutton"),
    ControlInfo("step_down", "Step Down", "pushbutton"),
    ControlInfo("recall_max_level", "Recall Max Level", "pushbutton"),
    ControlInfo("recall_min_level", "Recall Min Level", "pushbutton"),
    ControlInfo("step_down_and_off", "Step Down And Off", "pushbutton"),
    ControlInfo("on_and_step_up", "On And Step Up", "pushbutton"),
]


def get_common_controls() -> list[ControlInfo]:
    controls = [
        ControlInfo(
            id=descriptor.control_id,
            title=descriptor.title,
            type=descriptor.control_type,
            value=descriptor.default_value,
            read_only=True,
        )
        for descriptor in POLLING_CONTROLS
    ]
    controls.extend(PUSHBUTTON_CONTROLS)
    return controls


async def register_common_handlers(
    device: DaliDevice,
    controller: "ApplicationController",
    device_publisher: DevicePublisher,
) -> None:
    device_id = device.uid
    short_addr = GearShort(device.address.short)

    command_mapping = {
        "off": Off,
        "up": Up,
        "down": Down,
        "step_up": StepUp,
        "step_down": StepDown,
        "recall_max_level": RecallMaxLevel,
        "recall_min_level": RecallMinLevel,
        "step_down_and_off": StepDownAndOff,
        "on_and_step_up": OnAndStepUp,
    }

    def make_handler(cmd_class):
        async def handler(_msg):
            await controller.send_command(cmd_class(short_addr))

        return handler

    await asyncio.gather(
        *[
            device_publisher.register_control_handler(device_id, control_id, make_handler(command_class))
            for control_id, command_class in command_mapping.items()
        ]
    )


def build_polling_queries(devices: Iterable[DaliDevice]) -> list:
    if not POLLING_CONTROLS:
        return []
    queries: list = []
    for device in devices:
        for descriptor in POLLING_CONTROLS:
            queries.append(descriptor.query_builder(device))
    return queries


async def publish_polling_results(
    devices: Sequence[DaliDevice],
    responses: Sequence[Optional[Response]],
    device_publisher: DevicePublisher,
) -> None:
    per_device = get_polling_control_count()
    if per_device == 0:
        return

    tasks = []
    response_iter = iter(responses)

    for device in devices:
        device_responses = [next(response_iter, None) for _ in range(per_device)]

        for descriptor, response in zip(POLLING_CONTROLS, device_responses):
            if response is None or response.raw_value is None:
                tasks.append(device_publisher.set_control_error(device.uid, descriptor.control_id, "r"))
                continue

            if descriptor.control_type == "alarm":
                alarm_title = descriptor.value_formatter(response)
                alarm_active = "1" if getattr(response, "error", False) else "0"
                tasks.append(
                    device_publisher.set_control_title(device.uid, descriptor.control_id, alarm_title)
                )
                tasks.append(
                    device_publisher.set_control_value(
                        device.uid,
                        descriptor.control_id,
                        alarm_active,
                    )
                )
                continue

            tasks.append(
                device_publisher.set_control_value(
                    device.uid,
                    descriptor.control_id,
                    descriptor.value_formatter(response),
                )
            )

    if tasks:
        await asyncio.gather(*tasks)
