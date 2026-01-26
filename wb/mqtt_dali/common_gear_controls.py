import asyncio
from typing import Iterable, Optional, Sequence

from dali.address import GearShort
from dali.command import Response
from dali.gear.general import (
    Down,
    Off,
    OnAndStepUp,
    QueryActualLevel,
    RecallMaxLevel,
    RecallMinLevel,
    StepDown,
    StepDownAndOff,
    StepUp,
    Up,
)

from .dali_device import DaliDevice
from .device_publisher import DevicePublisher


def get_common_controls() -> list[dict]:
    return [
        {
            "id": "actual_level",
            "title": "Actual Level",
            "type": "value",
            "value": "0",
            "read_only": True,
        },
        {
            "id": "off",
            "title": "Off",
            "type": "pushbutton",
        },
        {
            "id": "up",
            "title": "Up",
            "type": "pushbutton",
        },
        {
            "id": "down",
            "title": "Down",
            "type": "pushbutton",
        },
        {
            "id": "step_up",
            "title": "Step Up",
            "type": "pushbutton",
        },
        {
            "id": "step_down",
            "title": "Step Down",
            "type": "pushbutton",
        },
        {
            "id": "recall_max_level",
            "title": "Recall Max Level",
            "type": "pushbutton",
        },
        {
            "id": "recall_min_level",
            "title": "Recall Min Level",
            "type": "pushbutton",
        },
        {
            "id": "step_down_and_off",
            "title": "Step Down And Off",
            "type": "pushbutton",
        },
        {
            "id": "on_and_step_up",
            "title": "On And Step Up",
            "type": "pushbutton",
        },
    ]


async def register_common_handlers(
    device: DaliDevice,
    controller: "ApplicationController",
    device_publisher: DevicePublisher,
) -> None:
    device_id = str(device.address.short)
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
        async def handler(msg):
            await controller.send_command(cmd_class(short_addr))

        return handler

    registration_tasks = [
        device_publisher.register_control_handler(device_id, control_id, make_handler(command_class))
        for control_id, command_class in command_mapping.items()
    ]

    await asyncio.gather(*registration_tasks)


def make_actual_level_query(device: DaliDevice) -> QueryActualLevel:
    short_addr = GearShort(device.address.short)
    return QueryActualLevel(short_addr)


def build_actual_level_queries(devices: Iterable[DaliDevice]) -> list[QueryActualLevel]:
    return [make_actual_level_query(device) for device in devices]


async def publish_actual_level_response(
    device: DaliDevice,
    response: Optional[Response],
    device_publisher: DevicePublisher,
) -> None:
    device_id = str(device.address.short)
    if response is not None:
        actual_level = str(response.raw_value.as_integer)
        await device_publisher.set_control_value(device_id, "actual_level", actual_level)
    else:
        await device_publisher.set_control_error(device_id, "actual_level", "r")


async def publish_actual_level_results(
    devices: Sequence[DaliDevice],
    responses: Sequence[Optional[Response]],
    device_publisher: DevicePublisher,
) -> None:
    await asyncio.gather(
        *[
            publish_actual_level_response(device, response, device_publisher)
            for device, response in zip(devices, responses)
        ]
    )
