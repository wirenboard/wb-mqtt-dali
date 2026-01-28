from typing import Optional

import asyncio_mqtt as aiomqtt
from dali.address import DeviceShort
from dali.command import Command
from dali.device import light, occupancy, pushbutton
from dali.device.general import _Event
from dali.device.light import LightEvent
from dali.device.occupancy import OccupancyEvent
from dali.device.pushbutton import (
    ButtonPressed,
    ButtonReleased,
    DoublePress,
    LongPressRepeat,
    LongPressStart,
    LongPressStop,
    ShortPress,
)

from .dali_2_device import Dali2Device
from .device_publisher import ControlInfo


def get_occupancy_controls(instance_index: int) -> list[ControlInfo]:
    return [
        ControlInfo(
            id=f"occupied{instance_index}",
            title=f"Occupied {instance_index}",
            type="switch",
            read_only=True,
            order=instance_index * 10 + 2,
            value="0",
        ),
        ControlInfo(
            id=f"movement{instance_index}",
            title=f"Movement {instance_index}",
            type="switch",
            read_only=True,
            order=instance_index * 10 + 3,
            value="0",
        ),
    ]


def get_light_controls(instance_index: int) -> list[ControlInfo]:
    return [
        ControlInfo(
            id=f"illuminance{instance_index}",
            title=f"Illuminance {instance_index}",
            type="value",
            read_only=True,
            order=instance_index * 10 + 1,
            value="0",
        ),
    ]


def get_button_controls(instance_index: int) -> list[ControlInfo]:
    return [
        ControlInfo(
            id=f"button{instance_index}",
            title=f"Button {instance_index}",
            type="switch",
            read_only=True,
            order=instance_index * 10 + 1,
            value="0",
        ),
        ControlInfo(
            id=f"button{instance_index}",
            title=f"Long Press {instance_index}",
            type="switch",
            read_only=True,
            order=instance_index * 10 + 2,
            value="0",
        ),
        ControlInfo(
            id=f"short_press{instance_index}",
            title=f"Short Press {instance_index}",
            type="pushbutton",
            read_only=True,
            order=instance_index * 10 + 3,
            value="0",
        ),
        ControlInfo(
            id=f"double_press{instance_index}",
            title=f"Double Press {instance_index}",
            type="pushbutton",
            read_only=True,
            order=instance_index * 10 + 4,
            value="0",
        ),
    ]


def get_dali2_controls(device: Dali2Device) -> list[ControlInfo]:
    return_controls: list[ControlInfo] = []
    for instance in device.instances.values():
        if instance.instance_type == occupancy.instance_type:
            return_controls.extend(get_occupancy_controls(instance.instance_number.value))
        elif instance.instance_type == light.instance_type:
            return_controls.extend(get_light_controls(instance.instance_number.value))
        elif instance.instance_type == pushbutton.instance_type:
            return_controls.extend(get_button_controls(instance.instance_number.value))
    return return_controls


async def publish_event(
    mqtt_client: aiomqtt.Client, device: Dali2Device, control_id: str, value: str, retain: bool = True
) -> None:
    await mqtt_client.publish(
        f"/devices/{device.uid}/controls/{control_id}",
        value,
        retain=retain,
        qos=2,
    )


def get_device(devices: dict[DeviceShort, Dali2Device], command: _Event) -> Optional[Dali2Device]:
    device = devices.get(command.short_address)
    if device is not None:
        instance = device.instances.get(command.instance_number)
        if instance is not None:
            return device
    return None


async def publish_dali2_event(
    command: Command, devices: dict[DeviceShort, Dali2Device], mqtt_client: aiomqtt.Client
) -> None:
    if not isinstance(command, _Event) or command.instance_number is None or command.short_address is None:
        return

    if isinstance(command, LightEvent):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(
                mqtt_client, device, f"illuminance{command.instance_number}", str(command.illuminance)
            )
        return

    if isinstance(command, OccupancyEvent):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(
                mqtt_client, device, f"movement{command.instance_number}", "1" if command.movement else "0"
            )
            await publish_event(
                mqtt_client, device, f"occupied{command.instance_number}", "1" if command.occupied else "0"
            )
        return

    if isinstance(command, ButtonPressed):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(mqtt_client, device, f"button{command.instance_number}", "1")
        return

    if isinstance(command, ButtonReleased):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(mqtt_client, device, f"button{command.instance_number}", "0")
        return

    if isinstance(command, (LongPressStart, LongPressRepeat)):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(mqtt_client, device, f"long_press{command.instance_number}", "1")
        return

    if isinstance(command, LongPressStop):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(mqtt_client, device, f"long_press{command.instance_number}", "0")
        return

    if isinstance(command, ShortPress):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(
                mqtt_client, device, f"short_press{command.instance_number}", "1", retain=False
            )
        return

    if isinstance(command, DoublePress):
        device = get_device(devices, command)
        if device is not None:
            await publish_event(
                mqtt_client, device, f"double_press{command.instance_number}", "1", retain=False
            )
