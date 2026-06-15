import aiomqtt
from dali.address import DeviceShort, Instance

from .common_dali_device import MqttControl
from .device import absolute_input_device, feedback
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


def get_feedback_controls(feature_address: Instance, suffix: str, order_base: int) -> list[MqttControl]:
    title_tail = f" {suffix}" if suffix else ""
    return [
        MqttControl(
            ControlInfo(
                id=f"activate_feedback{suffix}",
                meta=ControlMeta(
                    "pushbutton",
                    TranslatedTitle(
                        f"Activate feedback{title_tail}",
                        f"Активировать обратную связь{title_tail}",
                    ),
                    read_only=False,
                    order=order_base,
                ),
                value="0",
            ),
            commands_builder=lambda short_address, _value, addr=feature_address: [
                feedback.ActivateFeedback(short_address, addr)
            ],
        ),
        MqttControl(
            ControlInfo(
                id=f"stop_feedback{suffix}",
                meta=ControlMeta(
                    "pushbutton",
                    TranslatedTitle(
                        f"Stop feedback{title_tail}",
                        f"Остановить обратную связь{title_tail}",
                    ),
                    read_only=False,
                    order=order_base + 1,
                ),
                value="0",
            ),
            commands_builder=lambda short_address, _value, addr=feature_address: [
                feedback.StopFeedback(short_address, addr)
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
