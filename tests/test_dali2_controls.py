"""Tests for the MQTT controls of DALI-2 input devices.

Every one of them is the representation of a single native ``python-dali`` event type of a
single instance: it recognises the event by its class and the instance number the wrapper
carries, and its value is that event read as-is. The publish goes through a real MQTT device
(see ``_control_publishing``), so the retain flag each control's type implies is observed on
the wire rather than inferred from its meta.
"""

from typing import Callable, NamedTuple

import pytest
from dali.address import DeviceShort
from dali.device import light, occupancy, pushbutton
from dali.device.general import _Event

from wb.mqtt_dali.common_dali_device import MqttControlBase, NotifyResult
from wb.mqtt_dali.dali2_controls import (
    get_absolute_input_device_controls,
    get_button_controls,
    get_general_purpose_sensor_controls,
    get_light_controls,
    get_occupancy_controls,
)
from wb.mqtt_dali.device import absolute_input_device, general_purpose_sensor
from wb.mqtt_dali.events import Dali2InputEvent

from ._control_publishing import ValueTopicPublish, publish_events

_INSTANCE = 3
# Another instance of the same input device: its events must leave our control alone.
_FOREIGN_INSTANCE = 5

# Bit 9 of a measurement event's 10-bit field flags the event type, not data.
_MEASUREMENT_FLAG = 1 << 9


class _Dali2Case(NamedTuple):
    """One DALI-2 event control: the builder declaring it, its id, the event it reacts to
    (built for a given instance number) and the value it takes from that event."""

    control_id: str
    controls_builder: Callable[[int], list[MqttControlBase]]
    event_builder: Callable[[int], _Event]
    expected_value: str
    retained: bool = True


def _occupancy_event(instance_number: int, movement: bool, occupied: bool) -> _Event:
    return occupancy.OccupancyEvent(
        short_address=DeviceShort(1),
        instance_number=instance_number,
        data=occupancy.OccupancyEvent.EventData(movement=movement, occupied=occupied),
    )


_DALI2_CASES = [
    _Dali2Case(
        f"illuminance{_INSTANCE}",
        get_light_controls,
        lambda instance_number: light.LightEvent(
            short_address=DeviceShort(1), instance_number=instance_number, data=345
        ),
        "345",
    ),
    _Dali2Case(
        f"movement{_INSTANCE}",
        get_occupancy_controls,
        lambda instance_number: _occupancy_event(instance_number, movement=True, occupied=False),
        "1",
    ),
    _Dali2Case(
        f"occupied{_INSTANCE}",
        get_occupancy_controls,
        lambda instance_number: _occupancy_event(instance_number, movement=False, occupied=True),
        "1",
    ),
    _Dali2Case(
        f"button{_INSTANCE}",
        get_button_controls,
        lambda instance_number: pushbutton.ButtonPressed(
            short_address=DeviceShort(1), instance_number=instance_number
        ),
        "1",
    ),
    _Dali2Case(
        f"long_press{_INSTANCE}",
        get_button_controls,
        lambda instance_number: pushbutton.LongPressStart(
            short_address=DeviceShort(1), instance_number=instance_number
        ),
        "1",
    ),
    _Dali2Case(
        f"short_press{_INSTANCE}",
        get_button_controls,
        lambda instance_number: pushbutton.ShortPress(
            short_address=DeviceShort(1), instance_number=instance_number
        ),
        "1",
        retained=False,
    ),
    _Dali2Case(
        f"double_press{_INSTANCE}",
        get_button_controls,
        lambda instance_number: pushbutton.DoublePress(
            short_address=DeviceShort(1), instance_number=instance_number
        ),
        "1",
        retained=False,
    ),
    _Dali2Case(
        f"position{_INSTANCE}",
        get_absolute_input_device_controls,
        lambda instance_number: absolute_input_device.PositionEvent(
            short_address=DeviceShort(1), instance_number=instance_number, data=200
        ),
        "200",
    ),
    _Dali2Case(
        f"measurement{_INSTANCE}",
        get_general_purpose_sensor_controls,
        lambda instance_number: general_purpose_sensor.MeasurementEvent(
            short_address=DeviceShort(1),
            instance_number=instance_number,
            data=_MEASUREMENT_FLAG | 257,
        ),
        "257",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _DALI2_CASES, ids=lambda case: case.control_id)
async def test_dali2_controls_take_values_from_their_events(case):
    """Each control is built for `_INSTANCE` and offered its own event type twice: once from
    another instance of the same device, which must leave it at its declared default, and once
    from its own, whose value must land in `control_info.state.value` under a request to
    publish. Publishing it then shows the value on the control's topic, unretained for the two
    momentary pushbutton controls (`short_press`/`double_press`) and retained for the rest.
    """
    control = next(c for c in case.controls_builder(_INSTANCE) if c.control_info.id == case.control_id)
    default_value = control.control_info.state.value

    foreign = Dali2InputEvent(case.event_builder(_FOREIGN_INSTANCE))
    assert control.notify(foreign) is NotifyResult.NOTHING_TO_PUBLISH
    assert control.control_info.state.value == default_value

    published = await publish_events(control, [Dali2InputEvent(case.event_builder(_INSTANCE))])

    assert control.control_info.state.value == case.expected_value
    assert published == [ValueTopicPublish(case.expected_value, case.retained)]
