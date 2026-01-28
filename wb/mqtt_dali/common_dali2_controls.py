import asyncio_mqtt as aiomqtt
from dali.address import DeviceShort
from dali.command import Command
from dali.device import light, occupancy, pushbutton
from dali.device.light import LightEvent
from dali.device.occupancy import OccupancyEvent
from dali.device.pushbutton import (
    ButtonPressed,
    ButtonReleased,
    LongPressRepeat,
    LongPressStart,
    LongPressStop,
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


async def publish_dali2_event(
    command: Command, devices: list[Dali2Device], mqtt_client: aiomqtt.Client
) -> None:
    if isinstance(command, LightEvent) and command.instance_number is not None:
        for device in devices:
            if DeviceShort(device.address.short) == command.short_address:
                instance = device.instances.get(command.instance_number)
                if instance is not None:
                    await mqtt_client.publish(
                        f"/devices/{device.uid}/controls/illuminance{instance.instance_number.value}",
                        str(command.illuminance),
                        retain=True,
                        qos=2,
                    )
    elif isinstance(command, OccupancyEvent) and command.instance_number is not None:
        for device in devices:
            if DeviceShort(device.address.short) == command.short_address:
                instance = device.instances.get(command.instance_number)
                if instance is not None:
                    await mqtt_client.publish(
                        f"/devices/{device.uid}/controls/movement{instance.instance_number.value}",
                        "1" if command.movement else "0",
                        retain=True,
                        qos=2,
                    )
                    await mqtt_client.publish(
                        f"/devices/{device.uid}/controls/occupied{instance.instance_number.value}",
                        "1" if command.occupied else "0",
                        retain=True,
                        qos=2,
                    )
    elif (
        isinstance(command, (ButtonPressed, LongPressStart, LongPressRepeat))
        and command.instance_number is not None
    ):
        for device in devices:
            if DeviceShort(device.address.short) == command.short_address:
                instance = device.instances.get(command.instance_number)
                if instance is not None:
                    await mqtt_client.publish(
                        f"/devices/{device.uid}/controls/button{instance.instance_number.value}",
                        "1",
                        retain=True,
                        qos=2,
                    )
    elif isinstance(command, (ButtonReleased, LongPressStop)) and command.instance_number is not None:
        for device in devices:
            if DeviceShort(device.address.short) == command.short_address:
                instance = device.instances.get(command.instance_number)
                if instance is not None:
                    await mqtt_client.publish(
                        f"/devices/{device.uid}/controls/button{instance.instance_number.value}",
                        "0",
                        retain=True,
                        qos=2,
                    )
