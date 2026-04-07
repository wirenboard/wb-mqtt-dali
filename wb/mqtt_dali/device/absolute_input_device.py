"""
Commands, responses and events from IEC 62386 part 302: "Input devices —
Absolute input devices"
"""

from __future__ import annotations

from dali import command, frame
from dali.device import general

# "2" corresponds with "Part 302", as per Table 4 of IEC 62386 part 103
instance_type = 2  # pylint: disable=C0103


class PositionEvent(general._Event):  # pylint: disable=protected-access
    _instance_type = instance_type
    _event_info = 0

    @classmethod
    def _register_subclass(cls, subclass):
        raise RuntimeError(
            "Called PositionEvent._register_subclass()! There should be no subclasses of PositionEvent."
        )

    @classmethod
    def from_event_data(cls, event_data: int):
        return PositionEvent

    @property
    def event_data(self):
        return self._event_info

    def _set_event_data(self, set_data: int, set_frame: frame.Frame):
        if not isinstance(set_data, int):
            raise ValueError("PositionEvent requires 'data' to be set as an 'int'")

        self._event_info = set_data

        set_frame[9:0] = set_data

    @property
    def position(self) -> int:
        return self._event_info


###############################################################################
# Commands from Part 303 Table 10 start here
###############################################################################


class _AbsoluteInputDeviceCommand(general._StandardInstanceCommand):  # pylint: disable=W0212
    """
    An extension of the standard commands, addressed to an absolute input control
    device instance
    """

    _opcode = None


class SetReportTimer(_AbsoluteInputDeviceCommand):
    """
    The Report Timer (T_repeat) sets the interval between "repeat" messages.
    These are sent regardless of the state of the input has not changed.

    Report Timer increments in intervals of 1 second, i.e. the raw value is the
    actual value, in seconds.
    """

    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x10


class SetDeadtimeTimer(_AbsoluteInputDeviceCommand):
    """
    If the Deadtime Timer is set, the instance shall not send out an event until
    the Deadtime Timer has expired. The Deadtime Timer is restarted every time
    an event is sent.

    NOTE: The purpose of the Deadtime Timer is to increase the effective bus
    bandwidth availability. It is not intended to be used as a hold timer.

    Deadtime Timer increments in intervals of 50 ms, i.e. the raw value needs
    to be multiplied by 50 ms to get the actual value.
    """

    inputdev = True
    uses_dtr0 = True
    sendtwice = True
    _opcode = 0x11


class QueryDeadtimeTimer(_AbsoluteInputDeviceCommand):
    """
    Gets the current value for Deadtime Timer

    See also: SetDeadtimeTimer

    Deadtime Timer increments in intervals of 50 ms, i.e. the raw value needs
    to be multiplied by 50 ms to get the actual value.
    """

    inputdev = True
    response = command.NumericResponse
    _opcode = 0x1D


class QueryReportTimer(_AbsoluteInputDeviceCommand):
    """
    Gets the current value for Report Timer

    See also: SetReportTimer

    Report Timer increments in intervals of 1 second, i.e. the raw value is the
    actual value, in seconds.
    """

    inputdev = True
    response = command.NumericResponse
    _opcode = 0x1E


class QuerySwitch(_AbsoluteInputDeviceCommand):
    inputdev = True
    response = command.YesNoResponse
    _opcode = 0x1F
