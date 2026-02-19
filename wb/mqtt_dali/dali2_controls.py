import asyncio_mqtt as aiomqtt
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

from .common_dali_device import MqttControl
from .device_publisher import ControlInfo
from .wbmqtt import ControlMeta


def get_occupancy_controls(instance_index: int) -> list[MqttControl]:
    return [
        MqttControl(
            ControlInfo(
                id=f"occupied{instance_index}",
                meta=ControlMeta(
                    "switch",
                    f"Occupied {instance_index}",
                    read_only=True,
                    order=instance_index * 10 + 2,
                ),
                value="0",
            )
        ),
        MqttControl(
            ControlInfo(
                id=f"movement{instance_index}",
                meta=ControlMeta(
                    "switch",
                    f"Movement {instance_index}",
                    read_only=True,
                    order=instance_index * 10 + 3,
                ),
                value="0",
            )
        ),
    ]


def get_light_controls(instance_index: int) -> list[MqttControl]:
    return [
        MqttControl(
            ControlInfo(
                id=f"illuminance{instance_index}",
                meta=ControlMeta(
                    title=f"Illuminance {instance_index}",
                    read_only=True,
                    order=instance_index * 10 + 1,
                ),
                value="0",
            ),
        )
    ]


def get_button_controls(instance_index: int) -> list[MqttControl]:
    return [
        MqttControl(
            ControlInfo(
                id=f"button{instance_index}",
                meta=ControlMeta(
                    "switch",
                    f"Button {instance_index}",
                    read_only=True,
                    order=instance_index * 10 + 1,
                ),
                value="0",
            )
        ),
        MqttControl(
            ControlInfo(
                id=f"long_press{instance_index}",
                meta=ControlMeta(
                    "switch",
                    f"Long Press {instance_index}",
                    read_only=True,
                    order=instance_index * 10 + 2,
                ),
                value="0",
            )
        ),
        MqttControl(
            ControlInfo(
                id=f"short_press{instance_index}",
                meta=ControlMeta(
                    "pushbutton",
                    f"Short Press {instance_index}",
                    read_only=True,
                    order=instance_index * 10 + 3,
                ),
                value="0",
            )
        ),
        MqttControl(
            ControlInfo(
                id=f"double_press{instance_index}",
                meta=ControlMeta(
                    "pushbutton",
                    f"Double Press {instance_index}",
                    read_only=True,
                    order=instance_index * 10 + 4,
                ),
                value="0",
            )
        ),
    ]


async def publish_event(
    mqtt_client: aiomqtt.Client, device_id: str, control_id: str, value: str, retain: bool = True
) -> None:
    await mqtt_client.publish(
        f"/devices/{device_id}/controls/{control_id}",
        value,
        retain=retain,
        qos=2,
    )


async def publish_dali2_event(command: _Event, device_mqtt_id: str, mqtt_client: aiomqtt.Client) -> None:

    if isinstance(command, LightEvent):
        await publish_event(
            mqtt_client, device_mqtt_id, f"illuminance{command.instance_number}", str(command.illuminance)
        )
        return

    if isinstance(command, OccupancyEvent):
        await publish_event(
            mqtt_client,
            device_mqtt_id,
            f"movement{command.instance_number}",
            "1" if command.movement else "0",
        )
        await publish_event(
            mqtt_client,
            device_mqtt_id,
            f"occupied{command.instance_number}",
            "1" if command.occupied else "0",
        )
        return

    if isinstance(command, ButtonPressed):
        await publish_event(mqtt_client, device_mqtt_id, f"button{command.instance_number}", "1")
        return

    if isinstance(command, ButtonReleased):
        await publish_event(mqtt_client, device_mqtt_id, f"button{command.instance_number}", "0")
        return

    if isinstance(command, (LongPressStart, LongPressRepeat)):
        await publish_event(mqtt_client, device_mqtt_id, f"long_press{command.instance_number}", "1")
        return

    if isinstance(command, LongPressStop):
        await publish_event(mqtt_client, device_mqtt_id, f"long_press{command.instance_number}", "0")
        return

    if isinstance(command, ShortPress):
        await publish_event(
            mqtt_client, device_mqtt_id, f"short_press{command.instance_number}", "1", retain=False
        )
        return

    if isinstance(command, DoublePress):
        await publish_event(
            mqtt_client, device_mqtt_id, f"double_press{command.instance_number}", "1", retain=False
        )
