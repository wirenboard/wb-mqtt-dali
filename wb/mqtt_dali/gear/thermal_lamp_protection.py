"""
Commands and responses from IEC 62386 part 222, Device Type 21
Thermal lamp protection
"""

from dali import command
from dali.gear.general import _StandardCommand


class _ThermalLampProtectionCommand(_StandardCommand):
    devicetype = 21


class FailureStatusResponse(command.BitmapResponse):
    bits = [
        None,
        None,
        None,
        None,
        None,
        "thermal lamp shutdown",
        "thermal lamp overload",
        None,
    ]


class QueryFailureStatus(_ThermalLampProtectionCommand):
    """Query the failure status of the thermal lamp protection."""

    response = FailureStatusResponse
    _cmdval = 0xF1
