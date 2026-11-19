"""Tests that the button "pressed" state is cleared by release-type events, but
only when a press was actually reported.

The retained `button{instance}` control holds the pressed state ("1" while
held, "0" once released). The `button_released` event is disabled by default,
so without an extra reset a short/double/long press would leave the control
stuck at "1". These tests assert that `ButtonReleased`, `ShortPress`,
`DoublePress` and `LongPressStop` clear `button{instance}` after a
`ButtonPressed`, that a still-held `LongPressStart` does not, and that a
release event with no preceding press asks for no publish (no retained-"0" spam).
"""

import unittest

from dali.address import DeviceShort
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

from wb.mqtt_dali.common_dali_device import NotifyResult
from wb.mqtt_dali.dali2_controls import (
    ButtonControl,
    LongPressControl,
    get_occupancy_controls,
)
from wb.mqtt_dali.events import Dali2InputEvent

from ._control_publishing import publish_events

_INSTANCE = 4


def _event(command_type) -> Dali2InputEvent:
    return Dali2InputEvent(command_type(short_address=DeviceShort(1), instance_number=_INSTANCE))


class ButtonEventResetTests(unittest.TestCase):
    def test_release_type_event_after_press_clears_state(self):
        """A press publishes "1"; every release-type event behind it publishes "0" — the short
        and the double press too, for which no released event is delivered."""
        for command_type in (ButtonReleased, ShortPress, DoublePress):
            with self.subTest(command_type=command_type.__name__):
                control = ButtonControl(_INSTANCE)

                control.notify(_event(ButtonPressed))
                self.assertIs(control.notify(_event(command_type)), NotifyResult.PUBLISH_STATE)
                self.assertEqual(control.control_info.state.value, "0")

    def test_long_press_stop_clears_state_after_press(self):
        """LongPressStart keeps the state set while held; LongPressStop clears it."""
        control = ButtonControl(_INSTANCE)

        control.notify(_event(ButtonPressed))
        self.assertIs(control.notify(_event(LongPressStart)), NotifyResult.NOTHING_TO_PUBLISH)
        self.assertEqual(control.control_info.state.value, "1")

        self.assertIs(control.notify(_event(LongPressStop)), NotifyResult.PUBLISH_STATE)
        self.assertEqual(control.control_info.state.value, "0")

    def test_long_press_repeat_keeps_pressed_state(self):
        """Long press repeats arrive while the button is still held, so they
        must not clear the pressed state; the following stop clears it."""
        control = ButtonControl(_INSTANCE)

        control.notify(_event(ButtonPressed))
        control.notify(_event(LongPressStart))
        self.assertIs(control.notify(_event(LongPressRepeat)), NotifyResult.NOTHING_TO_PUBLISH)
        self.assertEqual(control.control_info.state.value, "1")

        self.assertIs(control.notify(_event(LongPressStop)), NotifyResult.PUBLISH_STATE)
        self.assertEqual(control.control_info.state.value, "0")

    def test_release_without_press_publishes_nothing(self):
        """No release-type event clears the state when no "button pressed" event
        preceded it — every variant must leave `button{instance}` untouched, so
        there is no retained-"0" spam (in particular `ButtonReleased`, which has
        no other publish branch)."""
        for command_type in (ButtonReleased, ShortPress, DoublePress, LongPressStop):
            with self.subTest(command_type=command_type.__name__):
                control = ButtonControl(_INSTANCE)

                self.assertIs(control.notify(_event(command_type)), NotifyResult.NOTHING_TO_PUBLISH)

    def test_release_after_clear_is_not_republished(self):
        """Once cleared, a further release event does not republish "0"."""
        control = ButtonControl(_INSTANCE)

        control.notify(_event(ButtonPressed))
        control.notify(_event(ButtonReleased))

        self.assertIs(control.notify(_event(ShortPress)), NotifyResult.NOTHING_TO_PUBLISH)


def _occupancy_event(occupied: bool, repeat: bool) -> Dali2InputEvent:
    return Dali2InputEvent(
        OccupancyEvent(
            short_address=DeviceShort(1),
            instance_number=_INSTANCE,
            data=OccupancyEvent.EventData(movement=False, occupied=occupied, repeat=repeat),
        )
    )


class EventControlPublishPolicyTests(unittest.IsolatedAsyncioTestCase):
    """Whether a repeated event reaches the topic is the control's publish policy, applied by
    the MQTT device — so these go through a real one."""

    async def test_long_press_repeat_republishes(self):
        """A held button republishes "1" on every LongPressRepeat under the ALWAYS policy,
        while an occupancy control's repeated event is dropped by ON_CHANGE."""
        published = await publish_events(
            LongPressControl(_INSTANCE),
            [_event(LongPressStart), _event(LongPressRepeat), _event(LongPressRepeat)],
        )

        self.assertEqual([publish.payload for publish in published], ["1", "1", "1"])

        occupied = next(
            control
            for control in get_occupancy_controls(_INSTANCE)
            if control.control_info.id == f"occupied{_INSTANCE}"
        )

        occupancy_published = await publish_events(
            occupied,
            [_occupancy_event(occupied=True, repeat=False), _occupancy_event(occupied=True, repeat=True)],
        )

        self.assertEqual([publish.payload for publish in occupancy_published], ["1"])
