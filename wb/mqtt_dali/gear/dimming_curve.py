"""
Commands and responses from IEC 62386 part 218, Device Type 17
Dimming curve selection
"""

from dali import command
from dali.gear.general import _StandardCommand


class _DimmingCurveCommand(_StandardCommand):
    devicetype = 17


class QueryDimmingCurve(_DimmingCurveCommand):
    """Query the dimming curve currently in use.

    0 - standard
    1 - linear
    """

    response = command.Response
    _cmdval = 0xEE
