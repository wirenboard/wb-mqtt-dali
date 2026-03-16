import asyncio_mqtt as aiomqtt
from dali.address import DeviceShort
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
from .device import absolute_input_device, feedback, general_purpose_sensor
from .device_publisher import ControlInfo
from .wbmqtt import ControlMeta, TranslatedTitle


def get_occupancy_controls(instance_index: int) -> list[MqttControl]:
    return [
        MqttControl(
            ControlInfo(
                id=f"occupied{instance_index}",
                meta=ControlMeta(
                    "switch",
                    TranslatedTitle(f"Occupied {instance_index}", f"Занято {instance_index}"),
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
                    TranslatedTitle(f"Movement {instance_index}", f"Движение {instance_index}"),
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
                    title=TranslatedTitle(f"Illuminance {instance_index}", f"Освещённость {instance_index}"),
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
                    TranslatedTitle(f"Button {instance_index}", f"Кнопка {instance_index}"),
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
                    TranslatedTitle(f"Long Press {instance_index}", f"Длинное нажатие {instance_index}"),
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
                    TranslatedTitle(f"Short Press {instance_index}", f"Короткое нажатие {instance_index}"),
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
                    TranslatedTitle(f"Double Press {instance_index}", f"Двойное нажатие {instance_index}"),
                    read_only=True,
                    order=instance_index * 10 + 4,
                ),
                value="0",
            )
        ),
    ]


def get_absolute_input_device_controls(instance_index: int) -> list[MqttControl]:
    return [
        MqttControl(
            ControlInfo(
                id=f"position{instance_index}",
                meta=ControlMeta(
                    title=TranslatedTitle(f"Position {instance_index}", f"Положение {instance_index}"),
                    read_only=True,
                    order=instance_index * 10 + 1,
                ),
                value="0",
            ),
        ),
        MqttControl(
            ControlInfo(
                id=f"switch{instance_index}",
                meta=ControlMeta(
                    "switch",
                    TranslatedTitle(f"Switch {instance_index}", f"Переключатель {instance_index}"),
                    read_only=True,
                    order=instance_index * 10 + 2,
                ),
                value="0",
            ),
            query_builder=lambda short_address: absolute_input_device.QuerySwitch(
                DeviceShort(short_address), instance_index
            ),
        ),
    ]


def get_general_purpose_sensor_controls(instance_index: int) -> list[MqttControl]:
    return [
        MqttControl(
            ControlInfo(
                id=f"measurement{instance_index}",
                meta=ControlMeta(
                    title=TranslatedTitle(f"Measurement {instance_index}", f"Измерение {instance_index}"),
                    read_only=True,
                    order=instance_index * 10 + 1,
                ),
                value="0",
            ),
        ),
    ]


def get_feedback_controls(instance_index: int) -> list[MqttControl]:
    return [
        MqttControl(
            ControlInfo(
                id=f"activate_feedback{instance_index}",
                meta=ControlMeta(
                    "pushbutton",
                    TranslatedTitle(f"Activate feedback {instance_index}", f"Активировать обратную связь {instance_index}"),
                    order=instance_index * 10 + 1,
                ),
                value="0",
            ),
            commands_builder=lambda short_address, _: [
                feedback.ActivateFeedback(DeviceShort(short_address), instance_index)
            ],
        ),
        MqttControl(
            ControlInfo(
                id=f"stop_feedback{instance_index}",
                meta=ControlMeta(
                    "pushbutton",
                    TranslatedTitle(f"Stop feedback {instance_index}", f"Остановить обратную связь {instance_index}"),
                    order=instance_index * 10 + 2,
                ),
                value="0",
            ),
            commands_builder=lambda short_address, _: [
                feedback.StopFeedback(DeviceShort(short_address), instance_index)
            ],
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

    if isinstance(command, absolute_input_device.PositionEvent):
        await publish_event(
            mqtt_client, device_mqtt_id, f"position{command.instance_number}", str(command.position)
        )

    if isinstance(command, general_purpose_sensor.MeasurementEvent):
        await publish_event(
            mqtt_client, device_mqtt_id, f"measurement{command.instance_number}", str(command.measurement)
        )
