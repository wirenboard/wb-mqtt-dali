"""
Commands and responses from IEC 62386 part 217, Device Type 16
Thermal gear protection
"""

from dali import command
from dali.gear.general import _StandardCommand


class _ThermalGearProtectionCommand(_StandardCommand):
    devicetype = 16


class FailureStatusResponse(command.BitmapResponse):
    bits = [
        None,
        None,
        None,
        None,
        None,
        "thermal gear shutdown",
        "thermal gear overload",
        None,
    ]


class QueryFailureStatus(_ThermalGearProtectionCommand):
    """Query the failure status of the thermal gear protection."""

    response = FailureStatusResponse
    _cmdval = 0xF1
