from typing import Callable

from dali.address import Instance
from dali.device import light, occupancy, pushbutton
from dali.device.general import _Event

from .common_dali_device import MqttControl, MqttControlBase, NotifyResult
from .device import absolute_input_device, feedback, general_purpose_sensor
from .device_publisher import ControlInfo
from .events import BusEvent, Dali2InputEvent
from .wbmqtt import ControlMeta, ControlState, PublishPolicy, TranslatedTitle


class _InstanceEventControl(MqttControlBase):
    """A control whose value is one DALI-2 input event type of one instance, read as-is."""

    def __init__(
        self,
        control_info: ControlInfo,
        instance_index: int,
        event_type: type[_Event],
        to_value: Callable[[_Event], str],
    ) -> None:
        super().__init__(control_info)
        self._instance_index = instance_index
        self._event_type = event_type
        self._to_value = to_value

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, Dali2InputEvent) or not isinstance(event.event, self._event_type):
            return NotifyResult.NOTHING_TO_PUBLISH
        if event.event.instance_number != self._instance_index:
            return NotifyResult.NOTHING_TO_PUBLISH
        self.control_info.state.value = self._to_value(event.event)
        return NotifyResult.PUBLISH_STATE


def _event_control(  # pylint: disable=too-many-arguments, R0917
    control_id: str,
    title: TranslatedTitle,
    order: int,
    instance_index: int,
    event_type: type[_Event],
    to_value: Callable[[_Event], str],
    control_type: str = "value",
) -> _InstanceEventControl:
    return _InstanceEventControl(
        ControlInfo(
            id=control_id,
            state=ControlState(
                meta=ControlMeta(control_type, title, read_only=True, order=order),
                value="0",
            ),
        ),
        instance_index,
        event_type,
        to_value,
    )


def get_occupancy_controls(instance_index: int) -> list[MqttControlBase]:
    return [
        _event_control(
            f"occupied{instance_index}",
            TranslatedTitle(f"Occupied {instance_index}", f"Занято {instance_index}"),
            instance_index * 10 + 2,
            instance_index,
            occupancy.OccupancyEvent,
            lambda event: "1" if event.occupied else "0",
            control_type="switch",
        ),
        _event_control(
            f"movement{instance_index}",
            TranslatedTitle(f"Movement {instance_index}", f"Движение {instance_index}"),
            instance_index * 10 + 3,
            instance_index,
            occupancy.OccupancyEvent,
            lambda event: "1" if event.movement else "0",
            control_type="switch",
        ),
    ]


def get_light_controls(instance_index: int) -> list[MqttControlBase]:
    return [
        _event_control(
            f"illuminance{instance_index}",
            TranslatedTitle(f"Illuminance {instance_index}", f"Освещённость {instance_index}"),
            instance_index * 10 + 1,
            instance_index,
            light.LightEvent,
            lambda event: str(event.illuminance),
        )
    ]


class ButtonControl(MqttControlBase):
    """Owns its own "pressed" latch: a release-type event publishes "0" only if this control
    last published "1". Short press and friends are enabled by default while "button pressed"
    is not, so they must not republish a retained "0" out of nowhere."""

    _RELEASE_EVENTS = (
        pushbutton.ButtonReleased,
        pushbutton.ShortPress,
        pushbutton.DoublePress,
        pushbutton.LongPressStop,
    )

    def __init__(self, instance_index: int) -> None:
        self._instance_index = instance_index
        self._pressed = False
        super().__init__(
            ControlInfo(
                id=f"button{instance_index}",
                state=ControlState(
                    meta=ControlMeta(
                        "switch",
                        TranslatedTitle(f"Button {instance_index}", f"Кнопка {instance_index}"),
                        read_only=True,
                        order=instance_index * 10 + 1,
                    ),
                    value="0",
                ),
            )
        )

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, Dali2InputEvent) or event.event.instance_number != self._instance_index:
            return NotifyResult.NOTHING_TO_PUBLISH
        if isinstance(event.event, pushbutton.ButtonPressed):
            self._pressed = True
            self.control_info.state.value = "1"
            return NotifyResult.PUBLISH_STATE
        if isinstance(event.event, self._RELEASE_EVENTS) and self._pressed:
            self._pressed = False
            self.control_info.state.value = "0"
            return NotifyResult.PUBLISH_STATE
        return NotifyResult.NOTHING_TO_PUBLISH


class LongPressControl(MqttControlBase):
    def __init__(self, instance_index: int) -> None:
        self._instance_index = instance_index
        super().__init__(
            ControlInfo(
                id=f"long_press{instance_index}",
                state=ControlState(
                    meta=ControlMeta(
                        "switch",
                        TranslatedTitle(f"Long Press {instance_index}", f"Длинное нажатие {instance_index}"),
                        read_only=True,
                        order=instance_index * 10 + 2,
                    ),
                    value="0",
                    # LongPressRepeat must reach the wire while the button is held.
                    publish_policy=PublishPolicy.ALWAYS,
                ),
            )
        )

    def notify(self, event: BusEvent) -> NotifyResult:
        if not isinstance(event, Dali2InputEvent) or event.event.instance_number != self._instance_index:
            return NotifyResult.NOTHING_TO_PUBLISH
        if isinstance(event.event, (pushbutton.LongPressStart, pushbutton.LongPressRepeat)):
            self.control_info.state.value = "1"
            return NotifyResult.PUBLISH_STATE
        if isinstance(event.event, pushbutton.LongPressStop):
            self.control_info.state.value = "0"
            return NotifyResult.PUBLISH_STATE
        return NotifyResult.NOTHING_TO_PUBLISH


def get_button_controls(instance_index: int) -> list[MqttControlBase]:
    return [
        ButtonControl(instance_index),
        LongPressControl(instance_index),
        _event_control(
            f"short_press{instance_index}",
            TranslatedTitle(f"Short Press {instance_index}", f"Короткое нажатие {instance_index}"),
            instance_index * 10 + 3,
            instance_index,
            pushbutton.ShortPress,
            lambda _event: "1",
            control_type="pushbutton",
        ),
        _event_control(
            f"double_press{instance_index}",
            TranslatedTitle(f"Double Press {instance_index}", f"Двойное нажатие {instance_index}"),
            instance_index * 10 + 4,
            instance_index,
            pushbutton.DoublePress,
            lambda _event: "1",
            control_type="pushbutton",
        ),
    ]


def get_absolute_input_device_controls(instance_index: int) -> list[MqttControlBase]:
    return [
        _event_control(
            f"position{instance_index}",
            TranslatedTitle(f"Position {instance_index}", f"Положение {instance_index}"),
            instance_index * 10 + 1,
            instance_index,
            absolute_input_device.PositionEvent,
            lambda event: str(event.position),
        ),
        MqttControl(
            ControlInfo(
                id=f"switch{instance_index}",
                state=ControlState(
                    meta=ControlMeta(
                        "switch",
                        TranslatedTitle(f"Switch {instance_index}", f"Переключатель {instance_index}"),
                        read_only=True,
                        order=instance_index * 10 + 2,
                    ),
                    value="0",
                ),
            ),
        ),
    ]


def get_general_purpose_sensor_controls(instance_index: int) -> list[MqttControlBase]:
    return [
        _event_control(
            f"measurement{instance_index}",
            TranslatedTitle(f"Measurement {instance_index}", f"Измерение {instance_index}"),
            instance_index * 10 + 1,
            instance_index,
            general_purpose_sensor.MeasurementEvent,
            lambda event: str(event.measurement),
        )
    ]


def get_feedback_controls(feature_address: Instance, suffix: str, order_base: int) -> list[MqttControl]:
    title_tail = f" {suffix}" if suffix else ""
    return [
        MqttControl(
            ControlInfo(
                id=f"activate_feedback{suffix}",
                state=ControlState(
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
            ),
            commands_builder=lambda short_address, _value, addr=feature_address: [
                feedback.ActivateFeedback(short_address, addr)
            ],
        ),
        MqttControl(
            ControlInfo(
                id=f"stop_feedback{suffix}",
                state=ControlState(
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
            ),
            commands_builder=lambda short_address, _value, addr=feature_address: [
                feedback.StopFeedback(short_address, addr)
            ],
        ),
    ]
