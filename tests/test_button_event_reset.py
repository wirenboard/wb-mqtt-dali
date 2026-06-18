"""Tests that the button "pressed" state is cleared by release-type events, but
only when a press was actually reported.

The retained `button{instance}` control holds the pressed state ("1" while
held, "0" once released). The `button_released` event is disabled by default,
so without an extra reset a short/double/long press would leave the control
stuck at "1". These tests assert that `ButtonReleased`, `ShortPress`,
`DoublePress` and `LongPressStop` clear `button{instance}` after a
`ButtonPressed`, that a still-held `LongPressStart` does not, and that a
release event with no preceding press publishes nothing (no retained-"0" spam).
"""

import unittest
from unittest.mock import AsyncMock

from dali.address import DeviceShort, InstanceNumber
from dali.device import pushbutton
from dali.device.pushbutton import (
    ButtonPressed,
    ButtonReleased,
    DoublePress,
    LongPressRepeat,
    LongPressStart,
    LongPressStop,
    ShortPress,
)

from wb.mqtt_dali.dali2_device import InstanceParameters, publish_dali2_event

_DEVICE_ID = "wb-dali_1_1"
_INSTANCE = 4
_BUTTON_TOPIC = f"/devices/{_DEVICE_ID}/controls/button{_INSTANCE}"


def _instance() -> InstanceParameters:
    return InstanceParameters(InstanceNumber(_INSTANCE), pushbutton.instance_type)


def _published(mqtt_client: AsyncMock) -> dict[str, str]:
    return {call.args[0]: call.args[1] for call in mqtt_client.publish.await_args_list}


def _event(command_type):
    return command_type(short_address=DeviceShort(1), instance_number=_INSTANCE)


class ButtonEventResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_press_then_release_clears_state(self):
        """A press publishes "1"; the following release publishes "0"."""
        instance = _instance()
        mqtt_client = AsyncMock()

        await publish_dali2_event(_event(ButtonPressed), _DEVICE_ID, mqtt_client, instance)
        await publish_dali2_event(_event(ButtonReleased), _DEVICE_ID, mqtt_client, instance)

        self.assertEqual(_published(mqtt_client)[_BUTTON_TOPIC], "0")
        self.assertFalse(instance.button_pressed)

    async def test_short_press_clears_state_after_press(self):
        """A short press following a press clears `button{instance}` even though
        no released event is delivered for it."""
        instance = _instance()
        mqtt_client = AsyncMock()

        await publish_dali2_event(_event(ButtonPressed), _DEVICE_ID, mqtt_client, instance)
        await publish_dali2_event(_event(ShortPress), _DEVICE_ID, mqtt_client, instance)

        self.assertEqual(_published(mqtt_client)[_BUTTON_TOPIC], "0")

    async def test_double_press_clears_state_after_press(self):
        """A double press following a press clears `button{instance}`."""
        instance = _instance()
        mqtt_client = AsyncMock()

        await publish_dali2_event(_event(ButtonPressed), _DEVICE_ID, mqtt_client, instance)
        await publish_dali2_event(_event(DoublePress), _DEVICE_ID, mqtt_client, instance)

        self.assertEqual(_published(mqtt_client)[_BUTTON_TOPIC], "0")

    async def test_long_press_stop_clears_state_after_press(self):
        """LongPressStart keeps the state set while held; LongPressStop clears it."""
        instance = _instance()
        mqtt_client = AsyncMock()

        await publish_dali2_event(_event(ButtonPressed), _DEVICE_ID, mqtt_client, instance)
        await publish_dali2_event(_event(LongPressStart), _DEVICE_ID, mqtt_client, instance)
        self.assertTrue(instance.button_pressed)

        await publish_dali2_event(_event(LongPressStop), _DEVICE_ID, mqtt_client, instance)
        self.assertEqual(_published(mqtt_client)[_BUTTON_TOPIC], "0")

    async def test_long_press_repeat_keeps_pressed_state(self):
        """Long press repeats arrive while the button is still held, so they
        must not clear the pressed state; the following stop clears it."""
        instance = _instance()
        mqtt_client = AsyncMock()

        await publish_dali2_event(_event(ButtonPressed), _DEVICE_ID, mqtt_client, instance)
        await publish_dali2_event(_event(LongPressStart), _DEVICE_ID, mqtt_client, instance)
        await publish_dali2_event(_event(LongPressRepeat), _DEVICE_ID, mqtt_client, instance)
        self.assertTrue(instance.button_pressed)
        mqtt_client.reset_mock()

        await publish_dali2_event(_event(LongPressStop), _DEVICE_ID, mqtt_client, instance)

        self.assertEqual(_published(mqtt_client)[_BUTTON_TOPIC], "0")
        self.assertFalse(instance.button_pressed)

    async def test_release_without_press_publishes_nothing(self):
        """No release-type event clears the state when no "button pressed" event
        preceded it — every variant must leave `button{instance}` untouched, so
        there is no retained-"0" spam (in particular `ButtonReleased`, which has
        no other publish branch)."""
        for command_type in (ButtonReleased, ShortPress, DoublePress, LongPressStop):
            with self.subTest(command_type=command_type.__name__):
                instance = _instance()
                mqtt_client = AsyncMock()

                await publish_dali2_event(_event(command_type), _DEVICE_ID, mqtt_client, instance)

                self.assertNotIn(_BUTTON_TOPIC, _published(mqtt_client))

    async def test_release_after_clear_is_not_republished(self):
        """Once cleared, a further release event does not republish "0"."""
        instance = _instance()
        mqtt_client = AsyncMock()

        await publish_dali2_event(_event(ButtonPressed), _DEVICE_ID, mqtt_client, instance)
        await publish_dali2_event(_event(ButtonReleased), _DEVICE_ID, mqtt_client, instance)
        mqtt_client.reset_mock()

        await publish_dali2_event(_event(ShortPress), _DEVICE_ID, mqtt_client, instance)

        self.assertNotIn(_BUTTON_TOPIC, _published(mqtt_client))
