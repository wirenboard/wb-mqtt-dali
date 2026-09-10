"""Driving a control's events through a real MQTT device.

Whether an update reaches the topic at all (a repeat under the control's publish policy) and
whether it is retained are ``wbmqtt.Device``'s decisions, not the control's, so a test about
them publishes through a real device over a mock client instead of reading the control's meta.
"""

from typing import Iterable, NamedTuple
from unittest.mock import AsyncMock, MagicMock

from wb.mqtt_dali.common_dali_device import MqttControlBase, NotifyResult
from wb.mqtt_dali.events import BusEvent
from wb.mqtt_dali.wbmqtt import Device

_DEVICE_MQTT_ID = "dev"


class ValueTopicPublish(NamedTuple):
    """One publish to a control's value topic."""

    payload: str
    retain: bool


async def publish_events(control: MqttControlBase, events: Iterable[BusEvent]) -> list[ValueTopicPublish]:
    """Declare ``control`` on an MQTT device at the value it starts from, then drive ``events``
    through it the way the event path does: whatever it answers ``PUBLISH_STATE`` to is
    published. Returns what reached its value topic, the initial declaration excluded.
    """
    client = MagicMock()
    client.publish = AsyncMock()
    device = Device(client, _DEVICE_MQTT_ID, "wb-mqtt-dali")
    control_id = control.control_info.id
    state = control.control_info.state
    await device.create_control(control_id, state.meta, state.value, state.publish_policy)
    client.publish.reset_mock()

    for event in events:
        if control.notify(event) is NotifyResult.PUBLISH_STATE:
            await device.set_control_state(control_id, state.value, state.error, state.meta.title)

    topic = f"/devices/{_DEVICE_MQTT_ID}/controls/{control_id}"
    return [
        ValueTopicPublish(call.args[1], call.kwargs["retain"])
        for call in client.publish.await_args_list
        if call.args[0] == topic
    ]
